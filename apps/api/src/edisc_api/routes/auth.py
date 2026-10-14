"""Browser sign-in, sign-out and the CSRF token (ADR 0016 §1-§3, Amendments 1 and 2; M17 step 2).

- ``GET /v1/auth/login``: tenant from the host; ``return_to`` validated (400 ``invalid_return_to``,
  never rewritten) and stored with the attempt; a login cookie (Lax) carries only a random handle;
  303 to the tenant's sign-in IdP (code + PKCE S256, state, nonce; ``prompt=login&max_age=0`` for a
  re-authentication). Starts are capped per client address and host (429).
- ``GET /v1/auth/callback``: the attempt is CONSUMED in the statement that finds it (single use),
  then handle, expiry and the IdP's answer are checked; the code is exchanged server-side and the ID
  token verified; the principal resolved by the ADR 0013 rules. Success: a NEW session (the session
  the browser held when it started is revoked ``rotated`` in the same transaction), audited, and a
  200 bounce page (A1) that sets the Strict session cookie and navigates same-site to ``return_to``.
  Failure: the outcome recorded and audited (a code from a fixed list, never provider text), and the
  bounce page to ``/?auth_error=<code>``.
- ``POST /v1/auth/logout`` (cookie + CSRF): revokes the session, clears the cookie, 204.
- ``GET /v1/auth/csrf`` (cookie): ``{"csrf_token"}``, ``Cache-Control: no-store``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edisc_api import audit
from edisc_api.app import (
    CallerDep,
    RequestIdDep,
    Resources,
    ResourcesDep,
    check_auth_throttle,
    count_auth_failure,
)
from edisc_api.auth import AuthError
from edisc_api.client_ip import client_ip
from edisc_api.errors import ApiError
from edisc_api.oidc import IdToken, OidcError
from edisc_api.sessions import (
    BOUNCE_HEADERS,
    LOGIN_COOKIE,
    SESSION_COOKIE,
    ReturnToError,
    bounce_page,
    clear_cookie,
    new_token,
    pkce_challenge,
    set_cookie,
    stored_user_agent,
    token_hash,
    validate_return_to,
    well_formed,
)
from edisc_core.ids import new_id
from edisc_db.session import tenant_tx

router = APIRouter(prefix="/v1")
PUBLIC = {"x-permission": "public"}
AUTHENTICATED = {"x-permission": "authenticated"}
ANONYMOUS = "anonymous"  # actor of an audit event about a sign-in that resolved no principal

# what an IdP's ``error`` parameter is recorded as (never its text, never ``error_description``)
PROVIDER_ERRORS = frozenset({
    "access_denied", "login_required", "interaction_required", "consent_required",
    "account_selection_required", "invalid_request", "unauthorized_client", "server_error",
    "temporarily_unavailable",
})  # fmt: skip
FAILURE_STATUS = {
    "denied": 400, "expired": 400, "invalid": 400, "unknown_user": 401, "inactive": 401,
    "reauth_mismatch": 403,
}  # fmt: skip


async def _no_crash(point: str) -> None:
    return None


# Test seam (the crash points of plan §8): called at named points of the callback.
crash_point: Callable[[str], Awaitable[None]] = _no_crash


@dataclass(frozen=True)
class Attempt:
    """The consumed login attempt, as the sign-in needs it."""

    id: uuid.UUID
    idp_id: uuid.UUID
    return_to: str
    reauth: bool
    prior_session_id: uuid.UUID | None
    started_at: datetime


class CsrfOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csrf_token: str


def _host(request: Request) -> str | None:
    return request.headers.get("host")


def redirect_uri(res: Resources, host: str | None) -> str:
    return f"{res.settings.api_public_scheme}://{(host or '').lower()}/v1/auth/callback"


async def _count_start(request: Request, res: Resources) -> None:
    """At most ``api_login_starts_per_window`` starts per (host, client address) per attempt
    lifetime: a burst cannot create unbounded ``login_attempts`` rows."""
    if res.redis is None:
        return
    client = client_ip(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        res.settings.api_trusted_proxies,
    )
    key = f"edisc:loginstart:{(_host(request) or '').lower()}:{client}"
    count = await res.redis.incr(key)
    if count == 1:
        await res.redis.expire(key, res.settings.api_login_attempt_seconds)
    if count > res.settings.api_login_starts_per_window:
        raise ApiError(429, "too_many_requests", "too many sign-ins started from this address")


async def _live_session(
    s: AsyncSession, session_id: str | None
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(session row id, principal id) of a live session for this cookie value, else None."""
    if session_id is None or not well_formed(session_id):
        return None
    row = (
        await s.execute(
            text(
                "SELECT s.id, s.principal_id FROM sessions s JOIN principals p ON p.id = s.principal_id"
                " WHERE s.id_hash = :h AND s.revoked_at IS NULL AND s.idle_expires_at > now()"
                " AND s.absolute_expires_at > now() AND p.active"
            ),
            {"h": token_hash(session_id)},
        )
    ).one_or_none()
    return (row.id, row.principal_id) if row is not None else None


@router.get("/auth/login", status_code=303, response_class=RedirectResponse, openapi_extra=PUBLIC)
async def login(
    request: Request,
    res: ResourcesDep,
    return_to: Annotated[str | None, Query()] = None,
    reauth: Annotated[bool, Query()] = False,
) -> Response:
    host = _host(request)
    await check_auth_throttle(request)
    tenant_id = await res.authenticator.tenant_for_host(host)  # unknown host: the generic 401
    try:
        target = validate_return_to(return_to)
    except ReturnToError as exc:
        raise ApiError(400, "invalid_return_to", "return_to must be a same-origin path") from exc
    await _count_start(request, res)
    async with tenant_tx(res.sessions, tenant_id) as s:
        idp = (
            await s.execute(
                text(
                    "SELECT id, issuer, login_client_id FROM tenant_idps"
                    " WHERE login_client_id IS NOT NULL"
                )
            )
        ).one_or_none()
        prior = await _live_session(s, request.cookies.get(SESSION_COOKIE))
    if idp is None:
        raise AuthError("no sign-in IdP for this tenant")  # the same 401 as an unknown host
    try:
        provider = await res.sign_in.provider(idp.issuer)
    except OidcError as exc:
        raise ApiError(502, "idp_unavailable", "the identity provider could not be used") from exc
    state, handle, nonce = new_token(), new_token(), new_token()
    kid = res.keys.current
    is_reauth = reauth and prior is not None  # without a live session it is a plain sign-in
    async with tenant_tx(res.sessions, tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO login_attempts (id, tenant_id, idp_id, state_hash, handle_hash, key_id,"
                " nonce, return_to, reauth, prior_session_id, expires_at) VALUES (:i, :t, :idp, :sh,"
                " :hh, :k, :n, :r, :re, :p, now() + make_interval(secs => :ttl))"
            ),
            {"i": new_id(), "t": tenant_id, "idp": idp.id, "sh": token_hash(state),
             "hh": token_hash(handle), "k": kid, "n": nonce, "r": target, "re": is_reauth,
             "p": prior[0] if prior else None, "ttl": res.settings.api_login_attempt_seconds},
        )  # fmt: skip
    url = res.sign_in.authorize_url(
        provider, client_id=idp.login_client_id, redirect_uri=redirect_uri(res, host), state=state,
        nonce=nonce, challenge=pkce_challenge(res.keys.pkce_verifier(kid, handle)),
        reauth=is_reauth,
    )  # fmt: skip
    response = RedirectResponse(url, status_code=303)
    response.headers["cache-control"] = "no-store"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers.append(
        "set-cookie",
        set_cookie(
            LOGIN_COOKIE, handle, max_age=res.settings.api_login_attempt_seconds, same_site="Lax"
        ),
    )
    return response


def _bounce(target: str, status: int, *cookies: str) -> HTMLResponse:
    response = HTMLResponse(bounce_page(target), status_code=status, headers=BOUNCE_HEADERS)
    for cookie in cookies:
        response.headers.append("set-cookie", cookie)
    return response


async def _fail(
    request: Request,
    res: Resources, tenant_id: uuid.UUID, attempt_id: uuid.UUID | None, code: str, rid: str,
    *, detail: str | None = None, principal: uuid.UUID | None = None,
) -> HTMLResponse:  # fmt: skip
    """Record the outcome (once), audit it with a fixed code, count it in the auth-failure throttle,
    bounce to ``/?auth_error=<code>``."""
    await count_auth_failure(request)
    async with tenant_tx(res.sessions, tenant_id) as s:
        if attempt_id is not None:
            await s.execute(
                text("UPDATE login_attempts SET outcome = :o WHERE id = :i AND outcome IS NULL"),
                {"o": code, "i": attempt_id},
            )
        await audit.record(
            s, tenant_id=tenant_id, actor=f"user:{principal}" if principal else ANONYMOUS,
            event_type="login_failed",
            payload={"reason": code, "attempt_id": str(attempt_id) if attempt_id else None,
                     **({"detail": detail} if detail else {})},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, tenant_id)
    return _bounce(f"/?auth_error={code}", FAILURE_STATUS[code], clear_cookie(LOGIN_COOKIE, "Lax"))


@router.get("/auth/callback", response_class=HTMLResponse, openapi_extra=PUBLIC)
async def callback(
    request: Request,
    res: ResourcesDep,
    rid: RequestIdDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> Response:
    host = _host(request)
    await check_auth_throttle(request)
    tenant_id = await res.authenticator.tenant_for_host(host)
    if state is None or not well_formed(state):
        return await _fail(request, res, tenant_id, None, "invalid", rid, detail="malformed_state")
    async with tenant_tx(res.sessions, tenant_id) as s:
        attempt = (
            await s.execute(
                text(
                    "UPDATE login_attempts SET consumed_at = now()"
                    " WHERE state_hash = :h AND consumed_at IS NULL"
                    " RETURNING id, idp_id, handle_hash, key_id, nonce, return_to, reauth,"
                    " prior_session_id, started_at, expires_at > now() AS fresh"
                ),
                {"h": token_hash(state)},
            )
        ).one_or_none()
    if attempt is None:  # unknown, replayed or another tenant's state
        return await _fail(
            request, res, tenant_id, None, "invalid", rid, detail="unknown_or_used_state"
        )
    await crash_point("consumed")
    handle = request.cookies.get(LOGIN_COOKIE)
    if handle is None or not well_formed(handle) or token_hash(handle) != attempt.handle_hash:
        return await _fail(
            request, res, tenant_id, attempt.id, "invalid", rid, detail="other_browser"
        )
    if not attempt.fresh:
        return await _fail(request, res, tenant_id, attempt.id, "expired", rid)
    if error is not None:
        recorded = error if error in PROVIDER_ERRORS else "other"
        outcome = "denied" if error == "access_denied" else "invalid"
        return await _fail(
            request, res, tenant_id, attempt.id, outcome, rid, detail=f"provider_{recorded}"
        )
    if code is None or not 1 <= len(code) <= 4096:
        return await _fail(request, res, tenant_id, attempt.id, "invalid", rid, detail="no_code")
    async with tenant_tx(res.sessions, tenant_id) as s:
        idp = (
            await s.execute(
                text(
                    "SELECT issuer, jwks_url, login_client_id, groups_claim FROM tenant_idps"
                    " WHERE id = :i AND login_client_id IS NOT NULL"
                ),
                {"i": attempt.idp_id},
            )
        ).one_or_none()
    if idp is None:
        return await _fail(
            request, res, tenant_id, attempt.id, "invalid", rid, detail="idp_removed"
        )
    try:
        provider = await res.sign_in.provider(idp.issuer)
        id_token = await res.sign_in.exchange(
            provider, client_id=idp.login_client_id, redirect_uri=redirect_uri(res, host),
            code=code, verifier=res.keys.pkce_verifier(attempt.key_id, handle),
        )  # fmt: skip
        claims = await res.sign_in.verify(
            id_token, issuer=idp.issuer, jwks_url=idp.jwks_url, client_id=idp.login_client_id,
            nonce=attempt.nonce, groups_claim=idp.groups_claim,
        )  # fmt: skip
    except (OidcError, AuthError, KeyError) as exc:
        detail = exc.code if isinstance(exc, OidcError) else type(exc).__name__
        return await _fail(
            request, res, tenant_id, attempt.id, "invalid", rid, detail=f"token_{detail}"
        )
    consumed = Attempt(
        attempt.id, attempt.idp_id, attempt.return_to, attempt.reauth, attempt.prior_session_id,
        attempt.started_at,
    )  # fmt: skip
    return await _sign_in(request, res, rid, tenant_id, consumed, idp.issuer, claims)


async def _sign_in(
    request: Request, res: Resources, rid: str, tenant_id: uuid.UUID, a: Attempt, issuer: str,
    claims: IdToken,
) -> Response:  # fmt: skip
    now = datetime.now(UTC)
    async with tenant_tx(res.sessions, tenant_id) as s:
        principal = (
            await s.execute(
                text("SELECT id, active FROM principals WHERE issuer = :i AND subject = :s"),
                {"i": issuer, "s": claims.subject},
            )
        ).one_or_none()
        prior = None
        if a.prior_session_id is not None:
            prior = (
                await s.execute(
                    text(
                        "SELECT principal_id FROM sessions WHERE id = :i AND revoked_at IS NULL"
                        " AND idle_expires_at > now() AND absolute_expires_at > now()"
                    ),
                    {"i": a.prior_session_id},
                )
            ).one_or_none()
    if principal is None:
        return await _fail(request, res, tenant_id, a.id, "unknown_user", rid)
    if not principal.active:
        return await _fail(request, res, tenant_id, a.id, "inactive", rid, principal=principal.id)
    if a.reauth and (
        prior is None
        or prior.principal_id != principal.id
        or claims.auth_time is None
        or claims.auth_time <= a.started_at
    ):
        # the old session is left exactly as it was (ADR 0016 §1)
        return await _fail(
            request, res, tenant_id, a.id, "reauth_mismatch", rid, principal=principal.id
        )
    authenticated_at = claims.auth_time or now
    session_value, session_row = new_token(), new_id()
    settings = res.settings
    absolute = now + timedelta(seconds=settings.api_session_absolute_seconds)
    idle = min(now + timedelta(seconds=settings.api_session_idle_seconds), absolute)
    actor = f"user:{principal.id}"
    async with tenant_tx(res.sessions, tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO sessions (id, tenant_id, principal_id, idp_id, id_hash, created_at,"
                " authenticated_at, last_seen_at, idle_expires_at, absolute_expires_at, idp_groups,"
                " user_agent, ip_first, request_id) VALUES (:i, :t, :p, :idp, :h, :now, :at, :now,"
                " :idle, :abs, :g, :ua, :ip, :rid)"
            ),
            {"i": session_row, "t": tenant_id, "p": principal.id, "idp": a.idp_id,
             "h": token_hash(session_value), "now": now, "at": authenticated_at, "idle": idle,
             "abs": absolute, "g": claims.groups, "ua": stored_user_agent(request.headers.get("user-agent")),
             "ip": request.client.host if request.client else None, "rid": rid},
        )  # fmt: skip
        rotated = None
        if prior is not None:
            rotated = (
                await s.execute(
                    text(
                        "UPDATE sessions SET revoked_at = now(), revoked_by = :by,"
                        " revoke_reason = 'rotated' WHERE id = :i AND revoked_at IS NULL RETURNING id"
                    ),
                    {"i": a.prior_session_id, "by": actor},
                )
            ).scalar_one_or_none()
        await s.execute(
            text("UPDATE login_attempts SET outcome = 'completed', session_id = :s WHERE id = :i"),
            {"s": session_row, "i": a.id},
        )
        await audit.record(
            s, tenant_id=tenant_id, actor=actor, event_type="session_created",
            payload={"session_id": str(session_row), "principal_id": str(principal.id),
                     "idp_id": str(a.idp_id), "authenticated_at": authenticated_at.isoformat(),
                     "reauth": bool(a.reauth),
                     "rotated_session_id": str(rotated) if rotated else None,
                     "user_agent": stored_user_agent(request.headers.get("user-agent"))},
            request_id=rid,
        )  # fmt: skip
        await crash_point("session_tx")
    await crash_point("session_committed")
    await audit.anchor(res.sessions, res.s3, res.settings, tenant_id)
    return _bounce(
        a.return_to,
        200,
        set_cookie(SESSION_COOKIE, session_value, max_age=settings.api_session_absolute_seconds,
                   same_site="Strict"),
        clear_cookie(LOGIN_COOKIE, "Lax"),
    )  # fmt: skip


def _session_value(request: Request) -> str:
    value = request.cookies.get(SESSION_COOKIE)
    if value is None:
        raise ApiError(400, "not_a_session", "this route is for browser sessions")
    return value


@router.post("/auth/logout", status_code=204, openapi_extra=AUTHENTICATED)
async def logout(
    request: Request, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> Response:
    _session_value(request)
    if caller.session_id is None:  # a cookie was sent, so the caller dependency used it
        raise ApiError(400, "not_a_session", "this route is for browser sessions")
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await s.execute(
            text(
                "UPDATE sessions SET revoked_at = now(), revoked_by = :by, revoke_reason = 'logout'"
                " WHERE id = :i AND revoked_at IS NULL"
            ),
            {"i": caller.session_id, "by": caller.actor},
        )
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="session_revoked",
            payload={"session_id": str(caller.session_id), "reason": "logout"}, request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    response = Response(status_code=204)
    response.headers.append("set-cookie", clear_cookie(SESSION_COOKIE, "Strict"))
    return response


@router.get("/auth/csrf", response_model=CsrfOut, openapi_extra=AUTHENTICATED)
async def csrf(
    request: Request, caller: CallerDep, res: ResourcesDep, response: Response
) -> CsrfOut:
    value = _session_value(request)
    response.headers["cache-control"] = "no-store"
    return CsrfOut(csrf_token=res.keys.csrf_token(value))
