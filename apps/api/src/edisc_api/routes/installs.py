"""Source install flows (M17 step 6; ADR 0016 §5, A2, A3, A4; plan §2).

- ``POST /v1/clients/{c}/connections/slack/install`` and ``.../teams/consent`` (``connection.manage``,
  a browser SESSION: the install is bound to its row): a ``pending`` connection (or the one to
  re-authorize), an ``oauth_installs`` row, the Lax ``__Host-edisc_install`` cookie holding a
  random handle, and ``{authorize_url, connection_id}``.
- ``GET /v1/oauth/{provider}/callback``:
  - on the RELAY host (``oauth.<base>``, the one URI registered with each provider): checks the
    state's format and expiry WITHOUT a database read, looks the install up
    (``oauth_install_route``: unconsumed, unexpired), recomputes the MAC from what the row says, and
    only then answers 303 to the SAME path on that row's tenant host with the query unchanged. It
    reads nothing else from the query, exchanges nothing, sets no cookie. Every refusal is one
    byte-identical page.
  - on a TENANT host: every check again (MAC, the row under this host's tenant, the atomic
    consume, the handle cookie, the bound session ROW still live, same principal, still
    ``connection.manage`` on the client), then the provider's server-side leg
    (``edisc_api.installs``), the cross-tenant binding (``bind_external``), the tokens (journal
    first, ``connection_tokens.install_tokens``) and the audit; answers the bounce page (A1) to the
    connection's page with ``?install=ok`` or ``?install_error=<code>``.
- Nothing about the tenant, the user, the destination or the external id is ever taken from the
  query: not ``redirect_uri``, ``return_to``, ``tenant``, ``team``, ``enterprise``,
  ``admin_consent``. Tokens, codes, states and verifiers never reach a log, an audit payload, a
  response or a URL we build.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError, RPCStatusCode

from edisc_api import audit, directory_refresh, installs
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import AuthError, Caller, caller_of_session_row
from edisc_api.authz import P, Scope, authorize, perm, public
from edisc_api.error_codes import E
from edisc_api.errors import ApiError, conflict, not_found
from edisc_api.installs import INSTALL_COOKIE, INSTALL_SECONDS, Grant, InstallError
from edisc_api.sessions import (
    BOUNCE_HEADERS,
    bounce_page,
    clear_cookie,
    new_token,
    set_cookie,
    token_hash,
    well_formed,
)
from edisc_core.ids import new_id
from edisc_core.logs import get_logger
from edisc_db.connection_tokens import TokenSet, install_tokens
from edisc_db.install_completion import InstallFacts, complete_install
from edisc_db.session import tenant_tx

log = get_logger(__name__)
router = APIRouter(prefix="/v1")

SOURCES = {"slack": "slack", "microsoft": "teams"}  # provider -> connection source
BINDINGS = {"slack": "slack_team", "microsoft": "microsoft_tenant"}
# what the UI receives as install_error (a fixed list)
ERRORS = ("denied", "expired", "replayed", "invalid", "mismatch", "already_bound", "failed",
          "refused")  # fmt: skip
REFUSED_PAGE = bounce_page("/?install_error=invalid")  # the relay's ONE refusal page


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallIn(Strict):
    connection_id: uuid.UUID | None = None  # re-authorize this connection (same client, source)


class InstallOut(Strict):
    authorize_url: str
    connection_id: uuid.UUID


# ------------------------------------------------------------------ start
async def _start(
    provider: str, client_id: uuid.UUID, body: InstallIn | None, caller: Caller, res: Resources,
    rid: str,
) -> JSONResponse:  # fmt: skip
    if caller.session_id is None:  # bound to a session row (A2): bearer callers cannot install
        raise ApiError(400, "not_a_session", "installs start from a browser session")
    source = SOURCES[provider]
    reconnect = body.connection_id if body is not None else None
    reauth = reconnect is not None
    handle, nonce, install_id = new_token(), new_token(), new_id()
    keys = res.keys
    try:
        requested = installs.requested_scopes(provider)
        # checked before anything is written (a missing app configuration refuses cleanly)
        installs.authorize_url(res.settings, provider, state="v1", nonce="n", verifier="v")
    except InstallError as exc:
        raise ApiError(503, "install_unavailable", "this provider is not configured") from exc
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CONNECTION_MANAGE, Scope("client", client_id))
        if reconnect is not None:
            row = (await s.execute(text(
                "SELECT client_id, source, status FROM connections WHERE id = :i"),
                {"i": reconnect})).one_or_none()  # fmt: skip
            if row is None or row.client_id != client_id or row.source != source:
                raise not_found()
            if row.status == "revoked":
                raise conflict("the connection is revoked")
            connection_id = reconnect
        else:
            connection_id = new_id()
            await s.execute(
                text(
                    "INSERT INTO connections (id, tenant_id, client_id, source, external_org_id,"
                    " status) VALUES (:i, :t, :c, :src, '', 'pending')"
                ),
                {"i": connection_id, "t": caller.tenant_id, "c": client_id, "src": source},
            )
        expires_at: datetime = (await s.execute(
            text("INSERT INTO oauth_installs (id, tenant_id, provider, client_id, connection_id,"
                 " reauth, session_id, principal_id, handle_hash, key_id, nonce, requested_scopes,"
                 " request_id, expires_at) VALUES (:i, :t, :p, :c, :conn, :re, :s, :pr, :h, :k, :n,"
                 " :sc, :rid, now() + make_interval(secs => :ttl)) RETURNING expires_at"),
            {"i": install_id, "t": caller.tenant_id, "p": provider, "c": client_id,
             "conn": connection_id, "re": reauth, "s": caller.session_id,
             "pr": caller.principal_id, "h": token_hash(handle), "k": keys.current, "n": nonce,
             "sc": list(requested), "rid": rid, "ttl": INSTALL_SECONDS},
        )).scalar_one()  # fmt: skip
        state = installs.sign_state(
            keys, install_id=install_id, expires_at=expires_at, tenant_id=caller.tenant_id,
            principal_id=caller.principal_id, nonce=nonce,
        )  # fmt: skip
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="oauth_install_started",
            payload={"provider": provider, "install_id": str(install_id),
                     "client_id": str(client_id), "connection_id": str(connection_id),
                     "reauth": reauth, "requested_scopes": list(requested),
                     "expires_at": expires_at.isoformat()},
            request_id=rid,
        )  # fmt: skip
    url = installs.authorize_url(
        res.settings, provider, state=state.text(), nonce=nonce,
        verifier=keys.install_verifier(keys.current, handle),
    )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    response = JSONResponse(
        InstallOut(authorize_url=url, connection_id=connection_id).model_dump(mode="json"),
        status_code=201,
    )
    response.headers.append(
        "set-cookie", set_cookie(INSTALL_COOKIE, handle, max_age=INSTALL_SECONDS, same_site="Lax")
    )
    return response


@router.post(
    "/clients/{client_id}/connections/slack/install", status_code=201, response_model=InstallOut,
    openapi_extra=perm(P.CONNECTION_MANAGE, E.FORBIDDEN, E.NOT_FOUND, E.CONFLICT, E.NOT_A_SESSION,
                       E.INSTALL_UNAVAILABLE),
)  # fmt: skip
async def start_slack_install(
    client_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep,
    body: InstallIn | None = None,
) -> JSONResponse:  # fmt: skip
    """Start installing our Slack app for a client (or re-authorize ``connection_id``)."""
    return await _start("slack", client_id, body, caller, res, rid)


@router.post(
    "/clients/{client_id}/connections/teams/consent", status_code=201, response_model=InstallOut,
    openapi_extra=perm(P.CONNECTION_MANAGE, E.FORBIDDEN, E.NOT_FOUND, E.CONFLICT, E.NOT_A_SESSION,
                       E.INSTALL_UNAVAILABLE),
)  # fmt: skip
async def start_teams_consent(
    client_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep,
    body: InstallIn | None = None,
) -> JSONResponse:  # fmt: skip
    """Start Microsoft admin consent (OIDC) for a client (or re-authorize ``connection_id``)."""
    return await _start("microsoft", client_id, body, caller, res, rid)


# ------------------------------------------------------------------ callback
@router.get(
    "/oauth/{provider}/callback", response_class=HTMLResponse,
    openapi_extra=public(E.INVALID, E.NOT_FOUND),
)  # fmt: skip
async def oauth_callback(provider: str, request: Request, res: ResourcesDep) -> Response:
    """The provider's redirect: relayed on the relay host, completed on the tenant host."""
    if provider not in installs.PROVIDERS:
        raise not_found()
    host = (request.headers.get("host") or "").split(":", 1)[0].lower()
    if host == installs.relay_host(res.settings):
        return await _relay(provider, request, res)
    return await _complete(provider, request, res)


def _refused() -> HTMLResponse:
    return HTMLResponse(REFUSED_PAGE, status_code=400, headers=BOUNCE_HEADERS)


async def _relay(provider: str, request: Request, res: Resources) -> Response:
    try:
        state = installs.parse_state(res.keys, request.query_params.get("state"))
    except InstallError:
        return _refused()  # malformed, unknown key or expired: no database read
    async with res.sessions() as s, s.begin():
        row = (await s.execute(
            text("SELECT * FROM oauth_install_route(:i)"), {"i": state.install_id}
        )).one_or_none()  # fmt: skip
    if (
        row is None or row.provider != provider or row.key_id != state.kid
        or int(row.expires_at.timestamp()) != state.expiry
        or not installs.mac_matches(res.keys, state, tenant_id=row.tenant_id,
                                    principal_id=row.principal_id, nonce=row.nonce)
    ):  # fmt: skip
        return _refused()
    # the destination comes from the row whose binding the MAC proves; the query passes unchanged
    target = (f"{res.settings.api_public_scheme}://{row.subdomain}.{res.settings.api_base_domain}"
              f"/v1/oauth/{provider}/callback")  # fmt: skip
    query = request.url.query
    return Response(status_code=303, headers={
        "location": f"{target}?{query}" if query else target,
        "referrer-policy": "no-referrer", "cache-control": "no-store",
    })  # fmt: skip


def _land(
    client_id: uuid.UUID | None, connection_id: uuid.UUID | None, error: str | None
) -> HTMLResponse:
    base = (f"/clients/{client_id}/connections/{connection_id}"
            if client_id is not None and connection_id is not None else "/")  # fmt: skip
    target = f"{base}?install_error={error}" if error else f"{base}?install=ok"
    response = HTMLResponse(bounce_page(target), status_code=200, headers=BOUNCE_HEADERS)
    response.headers.append("set-cookie", clear_cookie(INSTALL_COOKIE, "Lax"))
    return response


async def _complete(provider: str, request: Request, res: Resources) -> Response:
    rid: str = request.state.request_id
    try:
        tenant_id = await res.authenticator.tenant_for_host(request.headers.get("host"))
    except AuthError:
        return _refused()
    try:
        state = installs.parse_state(res.keys, request.query_params.get("state"))
    except InstallError as exc:  # expired states are recorded below when the row is known
        if exc.code != "expired":
            await _audit(res, tenant_id, None, rid, "oauth_install_invalid", {"detail": exc.detail})
            return _land(None, None, "invalid")
        state = installs.parse_state(res.keys, request.query_params.get("state"), now=0)
    async with tenant_tx(res.sessions, tenant_id) as s:
        row = (await s.execute(text("SELECT * FROM oauth_installs WHERE id = :i"),
                               {"i": state.install_id})).one_or_none()  # fmt: skip
    # another tenant's state finds no row here (RLS): refused, nothing touched in either tenant
    if (
        row is None
        or row.provider != provider
        or not installs.mac_matches(
            res.keys, state, tenant_id=tenant_id, principal_id=row.principal_id, nonce=row.nonce
        )
        or row.key_id != state.kid
        or int(row.expires_at.timestamp()) != state.expiry
    ):
        await _audit(res, tenant_id, None, rid, "oauth_install_invalid",
                     {"provider": provider, "detail": "state_unmatched"})  # fmt: skip
        return _land(None, None, "invalid")
    install = _Install(res, tenant_id, row, rid)
    # single use: consumed here, atomically, whatever happens next
    async with tenant_tx(res.sessions, tenant_id) as s:
        consumed = (await s.execute(text(
            "UPDATE oauth_installs SET consumed_at = now() WHERE id = :i AND consumed_at IS NULL"
            " RETURNING expires_at > now() AS live"), {"i": row.id})).one_or_none()  # fmt: skip
    if consumed is None:
        await install.audit_only("oauth_install_replayed", {"detail": "state_consumed"})
        return install.land("replayed")
    if not consumed.live:
        return await install.finish("expired", "expired", "state_expired")
    handle = request.cookies.get(INSTALL_COOKIE)
    if not well_formed(handle) or token_hash(handle or "") != bytes(row.handle_hash):
        return await install.finish("refused", "refused", "install_cookie_mismatch")
    async with tenant_tx(res.sessions, tenant_id) as s:
        actor = await caller_of_session_row(s, tenant_id, row.session_id)
        if actor is None or actor.principal_id != row.principal_id:
            return await install.finish("refused", "refused", "session_ended")
        try:
            await authorize(s, actor, P.CONNECTION_MANAGE, Scope("client", row.client_id))
        except ApiError:
            return await install.finish("refused", "refused", "permission_lost")
    install.actor = actor
    if request.query_params.get("error") is not None:  # the provider's own refusal (fixed codes)
        return await install.finish("denied", "denied", _provider_error(request))
    code = request.query_params.get("code")
    if not code or len(code) > 2048:
        return await install.finish("failed", "failed", "no_code")
    verifier = res.keys.install_verifier(row.key_id, handle or "")
    try:
        grant = await _exchange(provider, res, tenant_id, code, verifier, row.nonce)
    except InstallError as exc:
        return await install.finish(exc.code if exc.code in ERRORS else "failed",
                                    "refused" if exc.code == "refused" else "failed", exc.detail)  # fmt: skip
    return await install.complete(grant)


def _provider_error(request: Request) -> str:
    value = request.query_params.get("error") or ""
    return value if value in ("access_denied", "consent_required", "invalid_scope") else "other"


async def _exchange(provider: str, res: Resources, tenant_id: uuid.UUID, code: str,
                    verifier: str, nonce: str) -> Grant:  # fmt: skip
    http = res.authenticator.jwks.http
    if provider == "slack":
        return await installs.slack_exchange(
            res.settings, http, res.limiter, tenant_id=tenant_id, code=code, verifier=verifier
        )
    return await installs.microsoft_exchange(
        res.settings, http, res.limiter, res.authenticator.jwks, res.signer,
        tenant_id=tenant_id, code=code, verifier=verifier, nonce=nonce,
    )  # fmt: skip


async def _audit(res: Resources, tenant_id: uuid.UUID, actor: str | None, rid: str, event: str,
                 payload: dict[str, Any]) -> None:  # fmt: skip
    async with tenant_tx(res.sessions, tenant_id) as s:
        await audit.record(s, tenant_id=tenant_id, actor=actor or "anonymous", event_type=event,
                           payload=payload, request_id=rid)  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, tenant_id)


class _BoundElsewhere(Exception):  # noqa: N818 (a refusal, carried out of the transaction)
    def __init__(self, holder: uuid.UUID) -> None:
        super().__init__("external id bound to another tenant")
        self.holder = holder


async def _bind_one(s: AsyncSession, tenant_id: uuid.UUID, kind: str, external_id: str,
                    row: Any) -> None:  # fmt: skip
    holder: uuid.UUID = (await s.execute(
        text("SELECT bind_external(:k, :x, :c, :a)"),
        {"k": kind, "x": external_id, "c": row.connection_id, "a": f"user:{row.principal_id}"},
    )).scalar_one()  # fmt: skip
    if holder != tenant_id:
        raise _BoundElsewhere(holder)


class _Install:
    """One consumed install's ending: outcome on its row + audit (+ the connection when done)."""

    def __init__(self, res: Resources, tenant_id: uuid.UUID, row: Any, rid: str) -> None:
        self.res, self.tenant_id, self.row, self.rid = res, tenant_id, row, rid
        self.actor: Caller | None = None

    @property
    def base(self) -> dict[str, Any]:
        r = self.row
        return {"provider": r.provider, "install_id": str(r.id), "client_id": str(r.client_id),
                "connection_id": str(r.connection_id), "reauth": r.reauth}  # fmt: skip

    def land(self, error: str | None) -> HTMLResponse:
        return _land(self.row.client_id, self.row.connection_id, error)

    async def audit_only(self, event: str, extra: dict[str, Any]) -> None:
        await _audit(self.res, self.tenant_id, f"user:{self.row.principal_id}", self.rid, event,
                     {**self.base, **extra})  # fmt: skip

    async def _outcome(self, s: AsyncSession, outcome: str, detail: str | None,
                       external_id: str | None = None) -> None:  # fmt: skip
        await s.execute(text(
            "UPDATE oauth_installs SET outcome = :o, outcome_detail = :d, external_id = :x"
            " WHERE id = :i AND outcome IS NULL"),
            {"o": outcome, "d": detail, "x": external_id, "i": self.row.id})  # fmt: skip

    async def finish(self, error: str, outcome: str, detail: str,
                     extra: dict[str, Any] | None = None) -> HTMLResponse:  # fmt: skip
        """A refused or failed install: recorded, audited; the connection is unchanged."""
        event = {"expired": "oauth_install_expired", "denied": "oauth_install_denied",
                 "mismatch": "oauth_install_mismatch", "already_bound": "oauth_install_refused",
                 "refused": "oauth_install_refused"}.get(outcome, "oauth_install_failed")  # fmt: skip
        async with tenant_tx(self.res.sessions, self.tenant_id) as s:
            await self._outcome(s, outcome, detail)
            await audit.record(s, tenant_id=self.tenant_id,
                               actor=f"user:{self.row.principal_id}", event_type=event,
                               payload={**self.base, "detail": detail, **(extra or {})},
                               request_id=self.rid)  # fmt: skip
        await audit.anchor(self.res.sessions, self.res.s3, self.res.settings, self.tenant_id)
        log.info("install ended", provider=self.row.provider, outcome=outcome, detail=detail)
        return self.land(error)

    async def complete(self, grant: Grant) -> HTMLResponse:
        r, res = self.row, self.res
        async with tenant_tx(res.sessions, self.tenant_id) as s:
            conn = (await s.execute(text(
                "SELECT external_org_id, config->>'enterprise_id' AS enterprise_id, token_version,"
                " status FROM connections WHERE id = :i"), {"i": r.connection_id})).one()  # fmt: skip
        if r.reauth and (conn.external_org_id, conn.enterprise_id) != (
            grant.external_id,
            grant.enterprise_id,
        ):  # a re-authorization never re-points a connection at another workspace or tenant
            return await self.finish("mismatch", "mismatch", "external_id_differs", {
                "expected": [conn.external_org_id, conn.enterprise_id],
                "returned": [grant.external_id, grant.enterprise_id]})  # fmt: skip
        holder = await self._bind(grant)
        if holder is not None:
            await self._alert_bound(grant)
            return await self.finish("already_bound", "already_bound", "external_already_bound",
                                     {"external_id": grant.external_id})  # fmt: skip
        payload = {**self.base, "external_id": grant.external_id,
                   "enterprise_id": grant.enterprise_id, "granted_scopes": list(grant.scopes),
                   "blind_spots": list(grant.blind_spots), "token_type": grant.token_type,
                   "admin_evidence": grant.admin_evidence,
                   "authenticated_at": self.actor.authenticated_at.isoformat()
                   if self.actor is not None and self.actor.authenticated_at else None}  # fmt: skip

        facts = InstallFacts(
            r.id, r.connection_id, r.principal_id, self.rid, bool(r.reauth), grant.external_id,
            tuple(grant.scopes), tuple(grant.blind_spots),
            {"enterprise_id": grant.enterprise_id} if grant.enterprise_id else {},
            {**payload, "token_version": None},
        )  # fmt: skip

        async def finish_install(s: AsyncSession) -> None:
            await complete_install(s, facts)
            await audit.record(s, tenant_id=self.tenant_id, actor=f"user:{r.principal_id}",
                               event_type="oauth_install_completed", payload=facts.audit,
                               request_id=self.rid)  # fmt: skip

        if grant.access_token is not None:  # Slack: tokens journaled first, then applied
            await install_tokens(
                res.sessions, res.box, tenant_id=self.tenant_id, connection_id=r.connection_id,
                tokens=TokenSet(grant.access_token, grant.refresh_token, grant.expires_at,
                                conn.token_version),
                expected_version=conn.token_version, install=facts.as_journal(),
                with_tokens=finish_install,
            )  # fmt: skip
        else:  # Microsoft: no token is stored (client credentials on demand)
            async with tenant_tx(res.sessions, self.tenant_id) as s:
                await finish_install(s)
        await audit.anchor(res.sessions, res.s3, res.settings, self.tenant_id)
        if r.reauth:
            await self._resume()
        if res.settings.directory_refresh_on_connect:
            await directory_refresh.start(res.temporal, r.connection_id)
        log.info("install ended", provider=r.provider, outcome="completed")
        return self.land(None)

    async def _bind(self, grant: Grant) -> uuid.UUID | None:
        """Bind every external id to THIS tenant (one transaction: all or none). Returns the other
        tenant holding one of them, or None."""
        ids = [(BINDINGS[self.row.provider], grant.external_id)]
        if grant.enterprise_id:
            ids.append(("slack_enterprise", grant.enterprise_id))
        try:
            async with tenant_tx(self.res.sessions, self.tenant_id) as s:
                for kind, external_id in ids:
                    await _bind_one(s, self.tenant_id, kind, external_id, self.row)
        except _BoundElsewhere as exc:  # rolled back: nothing of this install stays bound
            return exc.holder
        return None

    async def _alert_bound(self, grant: Grant) -> None:
        """An external id held by another of our tenants: the requesting tenant's admins see an
        alert; operations see the error line (the other tenant is never named to this one)."""
        async with tenant_tx(self.res.sessions, self.tenant_id) as s:
            await s.execute(text(
                "INSERT INTO alerts (id, tenant_id, kind, message) VALUES (:i, :t,"
                " 'external_already_bound', :m)"),
                {"i": new_id(), "t": self.tenant_id,
                 "m": f"{self.row.provider} install refused: the {grant.external_id} it returned is"
                      " bound to another tenant (an operator can move it)"})  # fmt: skip
        log.error("operations alert: external id bound to another tenant",
                  provider=self.row.provider, install_id=str(self.row.id))  # fmt: skip

    async def _resume(self) -> None:
        from edisc_api.routes.connections import _pipeline

        resumed = await _pipeline(self.res).resume_connection(
            tenant_id=self.tenant_id, connection_id=self.row.connection_id,
            actor=f"user:{self.row.principal_id}",
        )  # fmt: skip
        for job_id in resumed:
            try:
                await self.res.temporal.get_workflow_handle(str(job_id)).signal("wake")
            except RPCError as exc:
                if exc.status is not RPCStatusCode.NOT_FOUND:
                    raise
