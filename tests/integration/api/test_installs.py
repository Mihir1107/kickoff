"""Source install flows (M17 step 6; ADR 0016 §5, A2, A3, A4, A2.3; plan §2).

The browser is ``sessions_flow.Browser`` (Strict cookies withheld on cross-site navigations, Lax
cookies sent): the provider's redirect reaches the RELAY host (``oauth.<base>``) and its 303 lands
on the tenant host as a cross-site navigation. Slack and Microsoft are ``providers.py`` fakes on
the production URLs; what they answer server-side is what a real provider's authenticated answer
would be, whatever the redirect's query says.
"""

from __future__ import annotations

import asyncio
import html
import re
import urllib.parse
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import event, text

from edisc_api import installs
from edisc_api.installs import sign_state
from edisc_db.connection_tokens import load_tokens
from edisc_db.session import tenant_tx
from tests.logcapture import captured_logs

from ..custody.conftest import superuser
from .conftest import Api, TenantCtx, add_principal, new_api_tenant
from .oidc import Browser
from .providers import FAKE_MICROSOFT, FAKE_SLACK
from .sessions_flow import call, csrf_token, enable_login, host_of, sign_in

PATHS = {"slack": "slack/install", "microsoft": "teams/consent"}
TID_A, TID_B = str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture
async def other(api: Api, kms: Any) -> TenantCtx:
    return await new_api_tenant(api, kms)


@pytest.fixture(autouse=True)
async def _providers(api: Api) -> None:
    """Fresh fakes per test; Microsoft trusts our published client keys."""
    FAKE_SLACK.codes.clear()
    FAKE_SLACK.issued.clear()
    FAKE_SLACK.exchanges = 0
    FAKE_MICROSOFT.codes.clear()
    FAKE_MICROSOFT.issued.clear()
    FAKE_MICROSOFT.refusals.clear()
    FAKE_MICROSOFT.consent_claims.clear()
    FAKE_MICROSOFT.client_jwks = await api.resources.signer.jwks()


async def _signed_in(api: Api, t: TenantCtx) -> Browser:
    await enable_login(api, t)
    browser = Browser()
    assert (await sign_in(api, t, browser)).status_code == 200
    return browser


async def start(api: Api, t: TenantCtx, provider: str, browser: Browser | None = None,
                connection_id: str | None = None) -> tuple[Browser, dict[str, Any]]:  # fmt: skip
    browser = browser or await _signed_in(api, t)
    body = {"connection_id": connection_id} if connection_id else {}
    r = await call(api, t, browser, "POST",
                   f"/v1/clients/{t.default_client_id}/connections/{PATHS[provider]}", json=body,
                   csrf=await csrf_token(api, t, browser))  # fmt: skip
    assert r.status_code == 201, r.text
    assert browser.get_cookie(host_of(api, t), installs.INSTALL_COOKIE) is not None
    return browser, r.json()


async def relay(api: Api, provider: str, query: dict[str, str] | str) -> httpx.Response:
    qs = query if isinstance(query, str) else urllib.parse.urlencode(query)
    async with api.client(api.settings.api_oauth_relay_subdomain) as c:
        return await c.get(f"/v1/oauth/{provider}/callback?{qs}",
                           headers={"sec-fetch-site": "cross-site"})  # fmt: skip


async def land(api: Api, t: TenantCtx, browser: Browser, location: str) -> str:
    """Follows the relay's 303 (a cross-site navigation: Lax cookies only); the bounce target."""
    parts = urllib.parse.urlsplit(location)
    assert parts.netloc == host_of(api, t), location
    r = await call(api, t, browser, "GET", f"{parts.path}?{parts.query}", site="cross-site")
    assert r.status_code == 200, r.text
    return _target(r.text)


def _target(page: str) -> str:
    found = re.search(r'content="0;url=([^"]+)"', page)
    assert found, page
    return html.unescape(found.group(1))


async def install(api: Api, t: TenantCtx, provider: str, *, browser: Browser | None = None,
                  connection_id: str | None = None, **at_provider: Any) -> tuple[str, dict[str, Any]]:  # fmt: skip
    """One whole install; the bounce target and the start answer."""
    browser, started = await start(api, t, provider, browser, connection_id)
    query = (FAKE_SLACK.authorize(started["authorize_url"], **at_provider) if provider == "slack"
             else FAKE_MICROSOFT.authorize(started["authorize_url"], **at_provider))  # fmt: skip
    hop = await relay(api, provider, query)
    assert hop.status_code == 303, hop.text
    return await land(api, t, browser, hop.headers["location"]), started


async def _connection(api: Api, t: TenantCtx, connection_id: str) -> Any:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return (await s.execute(text(
            "SELECT external_org_id, status, granted_scopes, blind_spots, config, token_version"
            " FROM connections WHERE id = :i"), {"i": uuid.UUID(connection_id)})).one()  # fmt: skip


async def _audits(api: Api, t: TenantCtx, prefix: str = "audit.oauth_install") -> list[Any]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return list((await s.execute(text(
            "SELECT event_type, payload FROM custody_events WHERE stream_id = :t"
            " AND event_type LIKE :p ORDER BY seq"), {"t": t.tenant_id, "p": prefix + "%"})).all())  # fmt: skip


async def _bindings(api: Api) -> dict[tuple[str, str], uuid.UUID]:
    conn = await superuser(api.settings)
    try:
        rows = await conn.fetch(
            "SELECT provider, external_id, bound_tenant_id FROM external_bindings"
        )
    finally:
        await conn.close()
    return {(r["provider"], r["external_id"]): r["bound_tenant_id"] for r in rows}


def _team() -> str:
    return "T" + uuid.uuid4().hex[:10].upper()


# ------------------------------------------------------------------ the happy paths
async def test_slack_binds_the_team_of_the_exchange_and_never_leaks_the_token(
    api: Api, tenant: TenantCtx
) -> None:
    team, scopes = _team(), ("channels:history", "channels:read", "users:read")
    with captured_logs() as logs:
        target, started = await install(api, tenant, "slack", team=team, scopes=scopes)
    cid = started["connection_id"]
    assert target == f"/clients/{tenant.default_client_id}/connections/{cid}?install=ok"
    row = await _connection(api, tenant, cid)
    assert (row.external_org_id, row.status) == (team, "active")
    assert list(row.granted_scopes) == sorted(scopes)  # what Slack GRANTED, not what we asked
    assert "private_channel_messages" in row.blind_spots
    stored = await load_tokens(api.sessions, api.resources.box, tenant_id=tenant.tenant_id,
                               connection_id=uuid.UUID(cid))  # fmt: skip
    token = FAKE_SLACK.issued[-1]
    assert stored.access_token.get_secret_value() == token
    assert (await _bindings(api))[("slack_team", team)] == tenant.tenant_id
    kinds = [a.event_type for a in await _audits(api, tenant)]
    assert kinds[-2:] == ["audit.oauth_install_started", "audit.oauth_install_completed"]
    # the token: nowhere in logs, audit payloads or anything the browser saw
    everything = logs.getvalue() + repr(await _audits(api, tenant, "audit.")) + target
    assert token not in everything and token[5:] not in everything


async def test_teams_binds_the_signed_id_tokens_tid_and_records_a_partial_grant(
    api: Api, tenant: TenantCtx
) -> None:
    tid = str(uuid.uuid4())
    with captured_logs() as logs:
        target, started = await install(api, tenant, "microsoft", tid=tid,
                                        roles=["ChannelMessage.Read.All", "Team.ReadBasic.All"])  # fmt: skip
    assert target.endswith("?install=ok"), target
    row = await _connection(api, tenant, started["connection_id"])
    assert (row.external_org_id, row.status) == (tid, "active")
    assert list(row.granted_scopes) == ["ChannelMessage.Read.All", "Team.ReadBasic.All"]
    assert "chats" in row.blind_spots and row.token_version == 0  # no Graph token stored
    assert (await _bindings(api))[("microsoft_tenant", tid)] == tenant.tenant_id
    for token in FAKE_MICROSOFT.issued:
        assert token not in logs.getvalue()
        assert token not in repr(await _audits(api, tenant, "audit."))


# ------------------------------------------------------------------ the Microsoft consent gate
async def _completed(api: Api, t: TenantCtx) -> dict[str, Any]:
    found = [a.payload for a in await _audits(api, t) if a.event_type.endswith("_completed")]
    assert len(found) == 1, found
    return dict(found[0])


async def _refused(api: Api, t: TenantCtx, target: str, started: dict[str, Any], tid: str,
                   detail: str) -> None:  # fmt: skip
    """Refused with ``detail``: nothing bound, the connection still pending, audited."""
    assert target.endswith("install_error=refused"), target
    row = await _connection(api, t, started["connection_id"])
    assert row.status == "pending" and not row.external_org_id  # a pending row: no identity
    assert ("microsoft_tenant", tid) not in await _bindings(api)
    outcomes = [a.payload for a in await _audits(api, t) if a.event_type.endswith("_refused")]
    assert [o["detail"] for o in outcomes] == [detail], outcomes


@pytest.mark.parametrize("wids", ["admin", "absent"])
async def test_teams_consent_is_proven_by_the_consent_token_with_or_without_wids(
    api: Api, tenant: TenantCtx, wids: str
) -> None:
    """The gate is Microsoft's admin consent (a client-credentials token for the tid, issued to our
    app, carrying the granted roles); wids is extra confirmation, never a requirement."""
    tid = str(uuid.uuid4())
    admin = True if wids == "admin" else None
    target, started = await install(api, tenant, "microsoft", tid=tid, admin=admin,
                                    roles=["ChannelMessage.Read.All"])  # fmt: skip
    assert target.endswith("?install=ok"), target
    assert (await _bindings(api))[("microsoft_tenant", tid)] == tenant.tenant_id
    evidence = await _completed(api, tenant)
    assert evidence["admin_evidence"] == (
        "consent_token+wids" if wids == "admin" else "consent_token"
    )


@pytest.mark.parametrize("wids", ["admin", "absent"])
async def test_teams_without_admin_consent_is_refused_with_or_without_wids(
    api: Api, tenant: TenantCtx, wids: str
) -> None:
    """No consent in the tenant (Microsoft issues no client-credentials token): refused, even
    when the ID token's wids names a Global Administrator; the missing wids skips nothing."""
    tid = str(uuid.uuid4())
    target, started = await install(api, tenant, "microsoft", tid=tid,
                                    admin=True if wids == "admin" else None, roles=None)  # fmt: skip
    await _refused(api, tenant, target, started, tid, "consent_token_refused")


@pytest.mark.parametrize(
    ("claims", "roles", "detail"),
    [
        ({}, [], "admin_consent_missing"),  # a token, but nothing granted
        ({"roles": None}, ["Chat.Read.All"], "admin_consent_missing"),  # no roles claim
        (
            {"tid": "00000000-0000-0000-0000-000000000001"},
            ["Chat.Read.All"],
            "consent_token_tenant",
        ),
        ({"appid": "another-app"}, ["Chat.Read.All"], "consent_token_app"),
    ],
)
async def test_a_consent_token_that_does_not_prove_consent_is_refused(
    api: Api, tenant: TenantCtx, claims: dict[str, Any], roles: list[str], detail: str
) -> None:
    tid = str(uuid.uuid4())
    FAKE_MICROSOFT.consent_claims[tid] = claims
    target, started = await install(api, tenant, "microsoft", tid=tid, admin=None, roles=roles)
    await _refused(api, tenant, target, started, tid, detail)


@pytest.mark.parametrize(
    ("wids", "detail"),
    [(["b79fbf4d-3ef9-4689-8143-76b194e85509"], "not_tenant_admin"),
     ("62e90394-69f5-4237-9190-012177145e10", "wids_malformed"), ([7], "wids_malformed")],
)  # fmt: skip
async def test_wids_when_present_must_confirm_an_administrator(
    api: Api, tenant: TenantCtx, wids: object, detail: str
) -> None:
    """Consent granted, but the ID token's wids contradicts an administrator: refused."""
    tid = str(uuid.uuid4())
    target, started = await install(api, tenant, "microsoft", tid=tid, wids=wids,
                                    roles=["ChannelMessage.Read.All"])  # fmt: skip
    await _refused(api, tenant, target, started, tid, detail)


# ------------------------------------------------------------------ A3: relay, redirects, state
async def test_the_relay_never_follows_a_redirect_named_in_the_query(
    api: Api, tenant: TenantCtx
) -> None:
    browser, started = await start(api, tenant, "slack")
    query = FAKE_SLACK.authorize(started["authorize_url"], team=_team())
    for name, value in (("redirect_uri", "https://evil.example/x"), ("return_to", "//evil.example"),
                        ("next", "https:%2F%2Fevil.example"), ("tenant", "other"), ("host", "evil.example")):  # fmt: skip
        query[name] = value
    hop = await relay(api, "slack", query)
    assert hop.status_code == 303
    location = urllib.parse.urlsplit(hop.headers["location"])
    assert (location.scheme, location.netloc, location.path) == (
        api.settings.api_public_scheme,
        host_of(api, tenant),
        "/v1/oauth/slack/callback",
    )
    assert hop.headers["referrer-policy"] == "no-referrer"
    target = await land(api, tenant, browser, hop.headers["location"])
    assert (
        target
        == f"/clients/{tenant.default_client_id}/connections/{started['connection_id']}?install=ok"
    )


async def test_a_replayed_state_is_refused_at_the_relay_and_the_tenant_host(
    api: Api, tenant: TenantCtx
) -> None:
    browser, started = await start(api, tenant, "slack")
    query = FAKE_SLACK.authorize(started["authorize_url"], team=_team())
    hop = await relay(api, "slack", query)
    assert (await land(api, tenant, browser, hop.headers["location"])).endswith("install=ok")
    again = await relay(api, "slack", query)
    assert again.status_code == 400 and "location" not in again.headers
    refused = (await relay(api, "slack", {"state": "nonsense"})).text
    assert again.text == refused  # one page whatever the reason (no enumeration)
    target = await land(api, tenant, browser, hop.headers["location"])
    assert target.endswith("install_error=replayed")
    assert (await _audits(api, tenant))[-1].event_type == "audit.oauth_install_replayed"
    assert FAKE_SLACK.exchanges == 1  # the replay never reached the provider


async def test_a_state_from_another_tenant_is_refused_and_nothing_is_stored(
    api: Api, tenant: TenantCtx, other: TenantCtx
) -> None:
    _, started = await start(api, tenant, "slack")
    query = FAKE_SLACK.authorize(started["authorize_url"], team=_team())
    browser_b = await _signed_in(api, other)
    # A's valid state straight on B's host (with B's own cookies): no row under B, refused
    target = await land(api, other, browser_b,
                        f"http://{host_of(api, other)}/v1/oauth/slack/callback?{urllib.parse.urlencode(query)}")  # fmt: skip
    assert target == "/?install_error=invalid"
    async with tenant_tx(api.sessions, other.tenant_id) as s:
        assert (await s.execute(text("SELECT count(*) FROM oauth_installs"))).scalar_one() == 0
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:  # A's install untouched
        row = (await s.execute(text("SELECT consumed_at FROM oauth_installs WHERE id = :i"),
                               {"i": uuid.UUID(query["state"].split(".")[2])})).one()  # fmt: skip
    assert row.consumed_at is None and FAKE_SLACK.exchanges == 0
    # a state whose MAC was computed for ANOTHER tenant (or principal): refused at the relay
    state = installs.parse_state(api.resources.keys, query["state"])
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        install = (await s.execute(text("SELECT * FROM oauth_installs WHERE id = :i"),
                                   {"i": state.install_id})).one()  # fmt: skip
    for tenant_id, principal_id in ((other.tenant_id, install.principal_id),
                                    (tenant.tenant_id, other.admin_id)):  # fmt: skip
        forged = sign_state(api.resources.keys, install_id=state.install_id,
                            expires_at=install.expires_at, tenant_id=tenant_id,
                            principal_id=principal_id, nonce=install.nonce)  # fmt: skip
        hop = await relay(api, "slack", {**query, "state": forged.text()})
        assert hop.status_code == 400


async def test_an_expired_state_is_refused_before_any_read_and_audited_at_the_tenant_host(
    api: Api, tenant: TenantCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from edisc_api.routes import installs as routes

    monkeypatch.setattr(routes, "INSTALL_SECONDS", 1)
    browser, started = await start(api, tenant, "slack")
    query = FAKE_SLACK.authorize(started["authorize_url"], team=_team())
    state = installs.parse_state(api.resources.keys, query["state"])
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:  # past it, by the database clock
        await s.execute(text("SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM expires_at - now())"
                             " + 0.2)) FROM oauth_installs WHERE id = :i"), {"i": state.install_id})  # fmt: skip
    statements: list[str] = []
    engine = api.sessions.kw["bind"].sync_engine
    listen = lambda *a: statements.append(a[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listen)
    try:
        hop = await relay(api, "slack", query)
    finally:
        event.remove(engine, "before_cursor_execute", listen)
    assert hop.status_code == 400 and statements == []  # refused on the state alone
    host = host_of(api, tenant)
    target = await land(api, tenant, browser,
                        f"http://{host}/v1/oauth/slack/callback?{urllib.parse.urlencode(query)}")  # fmt: skip
    assert target.endswith("install_error=expired")
    assert (await _audits(api, tenant))[-1].event_type == "audit.oauth_install_expired"
    assert FAKE_SLACK.exchanges == 0  # the code was never exchanged


async def test_a_forged_mac_and_an_unknown_install_are_refused(api: Api, tenant: TenantCtx) -> None:
    _, started = await start(api, tenant, "slack")
    query = FAKE_SLACK.authorize(started["authorize_url"], team=_team())
    kid, install_id, expiry, mac = query["state"].split(".")[1:]
    flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
    for state in (f"v1.{kid}.{install_id}.{expiry}.{flipped}",
                  f"v1.{kid}.{uuid.uuid4()}.{expiry}.{mac}"):  # fmt: skip
        assert (await relay(api, "slack", {**query, "state": state})).status_code == 400


# ------------------------------------------------------------------ A2: the browser and the session
async def test_the_install_needs_its_cookie_the_live_session_and_the_permission(
    api: Api, tenant: TenantCtx
) -> None:
    # without the install cookie (another browser holding only the link)
    _, started = await start(api, tenant, "slack")
    hop = await relay(api, "slack", FAKE_SLACK.authorize(started["authorize_url"], team=_team()))
    assert (await land(api, tenant, Browser(), hop.headers["location"])).endswith(
        "install_error=refused"
    )
    # the session revoked meanwhile
    browser, started = await start(api, tenant, "slack")
    hop = await relay(api, "slack", FAKE_SLACK.authorize(started["authorize_url"], team=_team()))
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        await s.execute(text("UPDATE sessions SET revoked_at = now(), revoked_by = 'tests',"
                             " revoke_reason = 'admin' WHERE revoked_at IS NULL"))  # fmt: skip
    assert (await land(api, tenant, browser, hop.headers["location"])).endswith(
        "install_error=refused"
    )
    details = [
        a.payload["detail"] for a in await _audits(api, tenant, "audit.oauth_install_refused")
    ]
    assert details == ["install_cookie_mismatch", "session_ended"]
    assert FAKE_SLACK.exchanges == 0  # nothing reached the provider's token endpoint


async def test_a_lost_permission_ends_the_install(api: Api, tenant: TenantCtx) -> None:
    pid, subject = await add_principal(api, tenant, roles=[("client_admin", "client",
                                                            tenant.default_client_id)])  # fmt: skip
    await enable_login(api, tenant)
    browser = Browser()
    assert (await sign_in(api, tenant, browser, subject)).status_code == 200
    _, started = await start(api, tenant, "slack", browser)
    hop = await relay(api, "slack", FAKE_SLACK.authorize(started["authorize_url"], team=_team()))
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        await s.execute(text("UPDATE role_assignments SET revoked_at = now(), revoked_by = 'tests'"
                             " WHERE principal_id = :p"), {"p": pid})  # fmt: skip
    assert (await land(api, tenant, browser, hop.headers["location"])).endswith(
        "install_error=refused"
    )
    assert (await _audits(api, tenant))[-1].payload["detail"] == "permission_lost"
    assert (await _connection(api, tenant, started["connection_id"])).status == "pending"


async def test_a_denial_leaves_the_connection_pending(api: Api, tenant: TenantCtx) -> None:
    target, started = await install(api, tenant, "slack", team=_team(), deny=True)
    assert target.endswith("install_error=denied")
    assert (await _connection(api, tenant, started["connection_id"])).status == "pending"
    assert (await _audits(api, tenant))[-1].payload["detail"] == "access_denied"


async def test_bearer_callers_cannot_start_an_install(api: Api, tenant: TenantCtx) -> None:
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        r = await c.post(
            f"/v1/clients/{tenant.default_client_id}/connections/slack/install", json={}
        )
    assert (r.status_code, r.json()["error"]) == (400, "not_a_session")


# ------------------------------------------------------------------ A4: the cross-customer attacks
async def test_microsoft_cross_customer_consent_binds_only_the_signed_tid(
    api: Api, tenant: TenantCtx, other: TenantCtx
) -> None:
    """B consents in its own Entra tenant and edits the redirect to name A's: B gets ITS tid. Then
    the provider's SIGNED answer naming a tid bound to A: refused in B, alerted, A untouched."""
    tid_a, tid_b = str(uuid.uuid4()), str(uuid.uuid4())
    assert (await install(api, tenant, "microsoft", tid=tid_a, roles=["Chat.Read.All"]))[
        0
    ].endswith("install=ok")
    browser, started = await start(api, other, "microsoft")
    query = FAKE_MICROSOFT.authorize(started["authorize_url"], tid=tid_b, roles=["Chat.Read.All"])
    query |= {"tenant": tid_a, "admin_consent": "True"}  # the attacker's edit
    hop = await relay(api, "microsoft", query)
    assert (await land(api, other, browser, hop.headers["location"])).endswith("install=ok")
    assert (await _connection(api, other, started["connection_id"])).external_org_id == tid_b
    # an admin of A's Entra tenant consents through B: Microsoft's signed token says tid_a
    target, started = await install(api, other, "microsoft", tid=tid_a, roles=["Chat.Read.All"])
    assert target.endswith("install_error=already_bound")
    assert (await _connection(api, other, started["connection_id"])).status == "pending"
    bindings = await _bindings(api)
    assert bindings[("microsoft_tenant", tid_a)] == tenant.tenant_id
    assert bindings[("microsoft_tenant", tid_b)] == other.tenant_id
    async with tenant_tx(api.sessions, other.tenant_id) as s:
        alerts = (await s.execute(text("SELECT count(*) FROM alerts WHERE kind ="
                                       " 'external_already_bound'"))).scalar_one()  # fmt: skip
    assert alerts == 1
    # not an administrator of the Entra tenant: refused
    target, _ = await install(api, other, "microsoft", tid=str(uuid.uuid4()), admin=False)
    assert target.endswith("install_error=refused")


async def test_slack_cross_customer_install_binds_only_the_exchanged_team(
    api: Api, tenant: TenantCtx, other: TenantCtx
) -> None:
    team_a, team_b, ent_a = _team(), _team(), "E" + uuid.uuid4().hex[:9].upper()
    assert (await install(api, tenant, "slack", team=team_a, enterprise=ent_a))[0].endswith(
        "install=ok"
    )
    browser, started = await start(api, other, "slack")
    query = FAKE_SLACK.authorize(started["authorize_url"], team=team_b)
    query |= {"team": team_a, "enterprise": ent_a, "team_id": team_a}  # the attacker's edit
    hop = await relay(api, "slack", query)
    assert (await land(api, other, browser, hop.headers["location"])).endswith("install=ok")
    assert (await _connection(api, other, started["connection_id"])).external_org_id == team_b
    # Slack's authenticated answer names A's team (or only A's enterprise): refused in B
    for team, enterprise in ((team_a, None), (_team(), ent_a)):
        target, started = await install(api, other, "slack", team=team, enterprise=enterprise)
        assert target.endswith("install_error=already_bound"), target
        assert (await _connection(api, other, started["connection_id"])).status == "pending"
    bindings = await _bindings(api)
    assert bindings[("slack_team", team_a)] == tenant.tenant_id
    assert bindings[("slack_enterprise", ent_a)] == tenant.tenant_id
    assert not any(k[1] not in (team_a, team_b, ent_a) and v == other.tenant_id
                   for k, v in bindings.items() if k[0].startswith("slack"))  # all-or-none  # fmt: skip


async def test_a_reauthorization_to_another_workspace_is_refused_and_keeps_the_token(
    api: Api, tenant: TenantCtx
) -> None:
    team = _team()
    _, started = await install(api, tenant, "slack", team=team)
    cid = started["connection_id"]
    before = await load_tokens(api.sessions, api.resources.box, tenant_id=tenant.tenant_id,
                               connection_id=uuid.UUID(cid))  # fmt: skip
    target, _ = await install(api, tenant, "slack", connection_id=cid, team=_team())
    assert target.endswith("install_error=mismatch")
    after = await load_tokens(api.sessions, api.resources.box, tenant_id=tenant.tenant_id,
                              connection_id=uuid.UUID(cid))  # fmt: skip
    assert after.access_token.get_secret_value() == before.access_token.get_secret_value()
    assert (await _connection(api, tenant, cid)).external_org_id == team
    # the same workspace re-authorizes: a new token, same connection
    target, _ = await install(api, tenant, "slack", connection_id=cid, team=team)
    assert target.endswith("install=ok")
    renewed = await load_tokens(api.sessions, api.resources.box, tenant_id=tenant.tenant_id,
                                connection_id=uuid.UUID(cid))  # fmt: skip
    assert renewed.version == before.version + 1


# ------------------------------------------------------------------ the registry (A4, A2.3)
async def test_the_app_role_cannot_touch_the_registry_except_through_the_bind(
    api: Api, tenant: TenantCtx
) -> None:
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        for statement in ("SELECT * FROM external_bindings",
                          "INSERT INTO external_bindings VALUES ('slack_team', 'TX', gen_random_uuid(),"
                          " gen_random_uuid(), now(), 'x')"):  # fmt: skip
            with pytest.raises(Exception, match="permission denied"):
                async with s.begin_nested():
                    await s.execute(text(statement))
    conn = await superuser(api.settings)
    try:
        callable_ = await conn.fetch(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'edisc' AND p.prokind = 'f'"
            " AND has_function_privilege('edisc_app', p.oid, 'EXECUTE')"
            " AND pg_get_functiondef(p.oid) ILIKE '%external_bindings%'"
        )
    finally:
        await conn.close()
    assert {r["proname"] for r in callable_} == {"bind_external"}


def test_no_route_moves_an_external_binding() -> None:
    """A2.3: moving an external id is an operator CLI action; no route of any environment writes
    the registry other than an install's bind."""
    import inspect

    from edisc_api import routes
    from edisc_api.app import create_app
    from edisc_core.settings import Environment, Settings

    for env in (Environment.TEST, Environment.PRODUCTION):
        paths = set(create_app(Settings.model_construct(env=env)).openapi()["paths"])
        assert not any("binding" in p or "external" in p for p in paths), paths
    sources = "".join(
        inspect.getsource(m) for m in (routes.installs, routes.connections, routes.admin)
    )
    assert "external_bindings" not in sources and sources.count("bind_external(") == 1


async def test_an_operator_moves_a_binding_after_the_source_revokes(
    api: Api, tenant: TenantCtx, other: TenantCtx
) -> None:
    from edisc_api.admin import MoveRefusedError, move_binding
    from edisc_db.session import create_engine, session_factory

    team = _team()
    _, started = await install(api, tenant, "slack", team=team)
    owner = create_engine(api.settings, "owner")
    try:
        move = {"provider": "slack_team", "external_id": team, "to_tenant": other.tenant_id,
                "reason": "the customer moved this workspace to its other account",
                "operator": "ops-1"}  # fmt: skip
        with pytest.raises(MoveRefusedError, match="revoke"):  # its connection is still live
            await move_binding(session_factory(owner), api.sessions, **move)
        with pytest.raises(MoveRefusedError, match="reason"):
            await move_binding(session_factory(owner), api.sessions, **{**move, "reason": " "})
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            await s.execute(text("UPDATE connections SET status = 'revoked' WHERE id = :i"),
                            {"i": uuid.UUID(started["connection_id"])})  # fmt: skip
        assert await move_binding(session_factory(owner), api.sessions, **move) == tenant.tenant_id
    finally:
        await owner.dispose()
    assert (await _bindings(api))[("slack_team", team)] == other.tenant_id
    for t, direction, peer in ((tenant, "moved_out", other), (other, "moved_in", tenant)):
        moved = (await _audits(api, t, "audit.external_binding_moved"))[-1].payload
        assert (moved["direction"], moved["other_tenant_id"]) == (direction, str(peer.tenant_id))
        async with tenant_tx(api.sessions, t.tenant_id) as s:
            assert (await s.execute(text("SELECT count(*) FROM alerts WHERE kind ="
                                         " 'external_binding_moved'"))).scalar_one() == 1  # fmt: skip
    target, _ = await install(api, other, "slack", team=team)  # now B's to install
    assert target.endswith("install=ok")


async def test_the_relay_subdomain_is_never_a_tenant(api: Api) -> None:
    from edisc_api.admin import onboard_tenant

    with pytest.raises(ValueError, match="reserved"):
        await onboard_tenant(api.sessions, kms_key_ref="k", subdomain="oauth", name="n",
                             issuer="https://i", audience="a", jwks_url="https://j",
                             admin_subject="s", admin_name="n", operator="tests")  # fmt: skip


async def _journal(api: Api, t: TenantCtx) -> int:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return int(
            (await s.execute(text("SELECT count(*) FROM token_refresh_journal"))).scalar_one()
        )


async def _reconcile(api: Api) -> dict[str, int]:
    """What a worker does at start (and on its schedule): its own sweeper engine."""
    from edisc_db.session import create_engine, session_factory
    from edisc_worker.install_recovery import reconcile_tokens

    sweeper = create_engine(api.settings, "sweeper")
    try:
        return await reconcile_tokens(
            session_factory(sweeper), api.sessions, api.resources.s3, api.settings
        )
    finally:
        await sweeper.dispose()


async def test_no_journal_entry_survives_an_install(api: Api, tenant: TenantCtx) -> None:
    """Plan §2.5: the tokens are journaled sealed before anything else, then the entry is deleted
    in the transaction that applies them: a completed install leaves no copy behind."""
    target, _ = await install(api, tenant, "slack", team=_team())
    assert target.endswith("?install=ok") and await _journal(api, tenant) == 0


@pytest.mark.parametrize("reauth", [False, True])
async def test_a_crash_after_the_journal_is_repaired_exactly_once(
    api: Api, tenant: TenantCtx, monkeypatch: pytest.MonkeyPatch, reauth: bool
) -> None:
    """A crash after the install's tokens were journaled and before they were applied: the
    reconciler applies them AND completes the install (connection active, outcome, audit) exactly
    once: two reconcilers racing and a rerun change nothing more; the journal is empty afterwards.
    A re-authorization bumps the existing connection's token version by exactly one."""
    from edisc_db import connection_tokens

    team = _team()
    browser = await _signed_in(api, tenant)
    connection_id: str | None = None
    if reauth:
        _, first = await install(api, tenant, "slack", browser=browser, team=team)
        connection_id = first["connection_id"]
    before = (await _connection(api, tenant, connection_id)).token_version if reauth else 0

    async def crash() -> None:
        raise RuntimeError("crash point: after the journal, before the apply")

    monkeypatch.setattr(connection_tokens, "_before_apply_hook", crash)
    browser, started = await start(api, tenant, "slack", browser, connection_id)
    hop = await relay(api, "slack", FAKE_SLACK.authorize(started["authorize_url"], team=team))
    parts = urllib.parse.urlsplit(hop.headers["location"])
    r = await call(api, tenant, browser, "GET", f"{parts.path}?{parts.query}", site="cross-site")
    assert r.status_code == 500 and FAKE_SLACK.issued[-1] not in r.text
    monkeypatch.undo()
    cid = started["connection_id"]
    assert await _journal(api, tenant) == 1
    assert (await _connection(api, tenant, cid)).token_version == before  # not applied

    first, second = await asyncio.gather(_reconcile(api), _reconcile(api))
    again = await _reconcile(api)
    assert first["installs"] + second["installs"] == 1 and again["installs"] == 0
    assert first["applied"] + second["applied"] == 1 and again["applied"] == 0
    assert await _journal(api, tenant) == 0

    row = await _connection(api, tenant, cid)
    assert (row.external_org_id, row.status, row.token_version) == (team, "active", before + 1)
    stored = await load_tokens(api.sessions, api.resources.box, tenant_id=tenant.tenant_id,
                               connection_id=uuid.UUID(cid))  # fmt: skip
    assert stored.access_token.get_secret_value() == FAKE_SLACK.issued[-1]
    completed = [a.payload for a in await _audits(api, tenant)
                 if a.event_type == "audit.oauth_install_completed"]  # fmt: skip
    recovered = [p for p in completed if p.get("recovered")]
    assert len(recovered) == 1 and recovered[0]["external_id"] == team
    assert FAKE_SLACK.issued[-1] not in repr(completed)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        outcomes = (await s.execute(text(
            "SELECT outcome FROM oauth_installs WHERE connection_id = :c ORDER BY started_at"),
            {"c": uuid.UUID(cid)})).scalars().all()  # fmt: skip
    assert outcomes[-1] == "completed"
