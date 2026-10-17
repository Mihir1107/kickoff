"""Browser sign-in and sessions (ADR 0016 §1-§4, Amendments 1 and 2; M17 step 2; plan §1, §7 T1-T8).

Sign-in runs through the test IdP (``oidc.py``): discovery and the code exchange answer server-side,
the browser leg is driven by the test. Cookies are kept by ``Browser`` (host-only, Strict cookies
withheld on the cross-site callback). Real-browser cookie delivery (A1) is ``test_sessions_browser``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from edisc_api.sessions import LOGIN_COOKIE, SESSION_COOKIE
from edisc_db.session import tenant_tx

from ..custody.conftest import superuser
from ..worker.conftest import spec
from .conftest import Api, TenantCtx, add_principal, new_api_tenant, secret
from .oidc import LOGIN_CLIENT, TEST_IDP, Browser, parse_set_cookie
from .sessions_flow import (
    call,
    csrf_token,
    enable_login,
    finish_login,
    host_of,
    login_cookie,
    session_cookie,
    sign_in,
    start_login,
)


@pytest.fixture
async def t(api: Api, tenant: TenantCtx) -> TenantCtx:
    await enable_login(api, tenant)
    return tenant


async def _audit(api: Api, t: TenantCtx, event_type: str) -> list[dict[str, Any]]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT actor, payload FROM custody_events WHERE stream_id = :t"
                    " AND event_type = :e ORDER BY seq"
                ),
                {"t": t.tenant_id, "e": f"audit.{event_type}"},
            )
        ).all()
    return [{"actor": r.actor, **r.payload} for r in rows]


async def _session_row(api: Api, t: TenantCtx, value: str) -> Any:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return (
            await s.execute(
                text("SELECT * FROM sessions WHERE id_hash = :h"),
                {"h": hashlib.sha256(value.encode()).digest()},
            )
        ).one_or_none()


def _cookie(browser: Browser, name: str) -> tuple[str, dict[str, str]]:
    (header,) = [h for h in browser.seen if h.startswith(f"{name}=") and "Max-Age=0" not in h][-1:]
    _, value, flags = parse_set_cookie(header)
    return value, flags


# ------------------------------------------------------------------ the sign-in itself
async def test_sign_in_sets_a_strict_host_only_session_through_a_same_site_bounce_page(
    api: Api, t: TenantCtx
) -> None:
    browser = Browser()
    started = await start_login(api, t, browser, return_to="/matters?tab=jobs")
    assert started.status_code == 303
    assert started.headers["location"].startswith("https://dev-idp.edisc.localhost/authorize?")
    assert started.headers["cache-control"] == "no-store"
    _, login_flags = _cookie(browser, LOGIN_COOKIE)
    assert login_flags.keys() >= {"secure", "httponly", "samesite", "path", "max-age"}
    assert (login_flags["samesite"], login_flags["path"], login_flags["max-age"]) == (
        "Lax",
        "/",
        "600",
    )
    assert "domain" not in login_flags
    query = dict(p.split("=", 1) for p in started.headers["location"].split("?", 1)[1].split("&"))
    assert query["client_id"] == LOGIN_CLIENT and query["code_challenge_method"] == "S256"
    assert "prompt" not in query  # a plain sign-in

    done = await finish_login(api, t, browser, started, subject=t.admin_subject)
    # A1: 200 with a static page whose meta refresh starts a NEW same-site navigation (not a 303)
    assert done.status_code == 200, done.text
    assert done.headers["content-type"].startswith("text/html")
    assert '<meta http-equiv="refresh" content="0;url=/matters?tab=jobs">' in done.text
    assert "<script" not in done.text.lower()
    csp = done.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert done.headers["referrer-policy"] == "no-referrer"
    assert done.headers["cache-control"] == "no-store"
    value, flags = _cookie(browser, SESSION_COOKIE)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", value)  # 256 bits
    assert flags.keys() >= {"secure", "httponly", "samesite", "path", "max-age"}
    assert (flags["samesite"], flags["path"]) == ("Strict", "/") and "domain" not in flags
    assert flags["max-age"] == str(api.settings.api_session_absolute_seconds)
    assert login_cookie(api, t, browser) is None  # deleted at the callback

    me = await call(api, t, browser, "GET", "/v1/me")
    assert me.status_code == 200 and me.json()["principal_id"] == str(t.admin_id)
    assert me.headers["cache-control"] == "no-store" and "Cookie" in me.headers["vary"]

    row = await _session_row(api, t, value)
    assert row is not None and row.principal_id == t.admin_id and row.revoked_at is None
    assert (row.absolute_expires_at - row.created_at).total_seconds() == 12 * 3600
    assert (row.idle_expires_at - row.created_at).total_seconds() == 30 * 60
    (created,) = await _audit(api, t, "session_created")
    assert created["session_id"] == str(row.id) and created["reauth"] is False
    assert created["actor"] == f"user:{t.admin_id}" and created["request_id"]
    everything_sql = (
        "SELECT payload FROM custody_events WHERE tenant_id = :t"
        " UNION ALL SELECT to_jsonb(a) FROM login_attempts a"
        " UNION ALL SELECT to_jsonb(x) - 'id_hash' FROM sessions x"
    )
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        rows = (await s.execute(text(everything_sql), {"t": t.tenant_id})).all()
    everything = json.dumps([list(map(str, r)) for r in rows])
    assert value not in everything  # the session id itself is stored nowhere, audited nowhere


async def test_a_session_cookie_answers_like_a_bearer_token_on_every_read(
    api: Api, t: TenantCtx
) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    r = await call(api, t, browser, "GET", f"/v1/clients/{t.default_client_id}")
    assert r.status_code == 200, r.text


async def test_the_pkce_verifier_is_never_stored_and_binds_the_code_to_this_browser(
    api: Api, t: TenantCtx
) -> None:
    """The attempt row holds hashes and the nonce only; the verifier is rebuilt from the handle."""
    browser = Browser()
    started = await start_login(api, t, browser)
    handle = login_cookie(api, t, browser)
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        row = (await s.execute(text("SELECT to_jsonb(a) AS j FROM login_attempts a"
                                    " ORDER BY started_at DESC LIMIT 1"))).one()  # fmt: skip
    assert handle is not None and handle not in json.dumps(row.j)
    assert set(row.j) >= {"state_hash", "handle_hash", "nonce", "key_id"}
    assert not {"pkce_verifier", "code_verifier", "verifier"} & set(row.j)
    done = await finish_login(api, t, browser, started, subject=t.admin_subject)
    assert done.status_code == 200 and TEST_IDP.token_calls == 1


async def test_unknown_host_login_is_the_generic_401(api: Api, t: TenantCtx) -> None:
    async with api.client("nosuchtenant") as c:
        unknown = await c.get("/v1/auth/login")
        callback = await c.get("/v1/auth/callback", params={"state": "x", "code": "y"})
    async with api.client(t.subdomain, "not-a-token") as c:
        bad_token = await c.get("/v1/me")
    assert unknown.status_code == callback.status_code == bad_token.status_code == 401
    assert unknown.content == callback.content == bad_token.content


async def test_a_tenant_without_a_sign_in_idp_answers_the_generic_401(
    api: Api, tenant: TenantCtx
) -> None:
    async with api.client(tenant.subdomain) as c:
        r = await c.get("/v1/auth/login")
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


# ------------------------------------------------------------------ return_to (T3)
@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/", "http://evil.example", "//evil.example", "/\\evil.example",
        "\\\\evil.example", "/%2F%2Fevil.example", "/%5Cevil.example", "%2F%2Fevil.example",
        "javascript:alert(1)", "evil.example/path", "/a\tb", "/a\nb", "/a%0Ab", "/a%00b",
        "/a\x7fb", "/" + "a" * 2048, "?x=1", "#frag",
    ],
)  # fmt: skip
async def test_return_to_that_is_not_a_same_origin_path_is_refused(
    api: Api, t: TenantCtx, value: str
) -> None:
    browser = Browser()
    r = await start_login(api, t, browser, return_to=value)
    assert (r.status_code, r.json()["error"]) == (400, "invalid_return_to"), r.text
    assert "location" not in r.headers and login_cookie(api, t, browser) is None
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        n = (await s.execute(text("SELECT count(*) FROM login_attempts"))).scalar_one()
    assert n == 0  # nothing stored, nothing followed


@pytest.mark.parametrize("value", ["/", "/matters/1?x=a%20b#top", "/a/b/c", "/%E2%9C%93"])
async def test_a_same_origin_path_round_trips(api: Api, t: TenantCtx, value: str) -> None:
    done = await sign_in(api, t, Browser(), return_to=value)
    assert done.status_code == 200
    assert f'content="0;url={value.replace("&", "&amp;")}"' in done.text


async def test_the_default_return_is_the_root(api: Api, t: TenantCtx) -> None:
    done = await sign_in(api, t, Browser())
    assert 'content="0;url=/"' in done.text


# ------------------------------------------------------------------ the callback's checks (T2, T9)
async def _attempts(api: Api, t: TenantCtx) -> list[Any]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return list(
            (await s.execute(text("SELECT * FROM login_attempts ORDER BY started_at"))).all()
        )


async def _no_session(api: Api, t: TenantCtx) -> None:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        n = (await s.execute(text("SELECT count(*) FROM sessions"))).scalar_one()
    assert n == 0


def _failed_to(done: Any, code: str, status: int) -> None:
    assert done.status_code == status, done.text
    assert f'content="0;url=/?auth_error={code}"' in done.text
    assert SESSION_COOKIE not in done.headers.get("set-cookie", "")


async def test_a_state_is_single_use(api: Api, t: TenantCtx) -> None:
    browser = Browser()
    started = await start_login(api, t, browser)
    path, query = TEST_IDP.authorize(started.headers["location"], subject=t.admin_subject)
    first = await call(api, t, browser, "GET", path, params=query, site="cross-site")
    assert first.status_code == 200
    browser.set(
        host_of(api, t),
        LOGIN_COOKIE,
        started.headers["set-cookie"].split(";")[0].split("=", 1)[1],
        "Lax",
    )
    again = await call(api, t, browser, "GET", path, params=query, site="cross-site")
    _failed_to(again, "invalid", 400)
    failed = await _audit(api, t, "login_failed")
    assert [f["detail"] for f in failed] == ["unknown_or_used_state"]


async def test_another_browsers_login_cookie_is_refused(api: Api, t: TenantCtx) -> None:
    """Login CSRF: the victim's browser completes a sign-in the attacker started."""
    attacker, victim = Browser(), Browser()
    started = await start_login(api, t, attacker)
    path, query = TEST_IDP.authorize(started.headers["location"], subject=t.admin_subject)
    await start_login(api, t, victim)  # the victim holds a login cookie of their own
    done = await call(api, t, victim, "GET", path, params=query, site="cross-site")
    _failed_to(done, "invalid", 400)
    await _no_session(api, t)
    assert TEST_IDP.token_calls == 0  # refused before the code is exchanged
    (attempt, _) = await _attempts(api, t)
    assert attempt.consumed_at is not None and attempt.outcome == "invalid"
    (failed,) = await _audit(api, t, "login_failed")
    assert failed["detail"] == "other_browser" and failed["attempt_id"] == str(attempt.id)


async def test_no_login_cookie_is_refused(api: Api, t: TenantCtx) -> None:
    browser = Browser()
    started = await start_login(api, t, browser)
    browser.drop(host_of(api, t), LOGIN_COOKIE)
    _failed_to(
        await finish_login(api, t, browser, started, subject=t.admin_subject), "invalid", 400
    )
    await _no_session(api, t)


async def test_an_expired_attempt_is_refused(api: Api, t: TenantCtx) -> None:
    browser = Browser()
    started = await start_login(api, t, browser)
    conn = await superuser(api.settings)
    try:
        await conn.execute(
            "UPDATE login_attempts SET expires_at = now() - interval '1 second' WHERE tenant_id = $1",
            t.tenant_id,
        )
    finally:
        await conn.close()
    _failed_to(
        await finish_login(api, t, browser, started, subject=t.admin_subject), "expired", 400
    )
    await _no_session(api, t)
    assert TEST_IDP.token_calls == 0


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [("access_denied", "denied", "provider_access_denied"),
     ("login_required", "invalid", "provider_login_required"),
     ("Zq-made-up <script>", "invalid", "provider_other")],
)  # fmt: skip
async def test_an_idp_error_is_recorded_as_a_code_never_its_text(
    api: Api, t: TenantCtx, error: str, code: str, detail: str
) -> None:
    browser = Browser()
    started = await start_login(api, t, browser)
    _, query = TEST_IDP.authorize(started.headers["location"], subject=t.admin_subject)
    done = await call(
        api, t, browser, "GET", "/v1/auth/callback", site="cross-site",
        params={"state": query["state"], "error": error, "error_description": "Zq-text"},
    )  # fmt: skip
    _failed_to(done, code, 400)
    assert error not in done.text and "Zq-text" not in done.text
    (failed,) = await _audit(api, t, "login_failed")
    assert failed["reason"] == code and failed["detail"] == detail
    assert "Zq" not in json.dumps(failed)


@pytest.mark.parametrize(
    "claims",
    [{"nonce": "not-the-nonce"}, {"aud": "another-client"}, {"iss": "https://other.example"},
     {"aud": [LOGIN_CLIENT, "x"], "azp": "x"}],
)  # fmt: skip
async def test_an_id_token_that_does_not_check_out_is_refused(
    api: Api, t: TenantCtx, claims: dict[str, Any]
) -> None:
    done = await sign_in(api, t, Browser(), **claims)
    _failed_to(done, "invalid", 400)
    await _no_session(api, t)


async def test_a_wrong_discovery_issuer_or_a_failed_exchange_is_refused(
    api: Api, t: TenantCtx
) -> None:
    browser = Browser()
    started = await start_login(api, t, browser)
    TEST_IDP.token_status = 500
    _failed_to(
        await finish_login(api, t, browser, started, subject=t.admin_subject), "invalid", 400
    )
    TEST_IDP.token_status = 200
    api.resources.sign_in._cache.clear()
    TEST_IDP.discovery_issuer = "https://evil.example"
    r = await start_login(api, t, Browser())
    assert (r.status_code, r.json()["error"]) == (502, "idp_unavailable")
    await _no_session(api, t)


async def test_an_unknown_or_inactive_user_gets_no_session(api: Api, t: TenantCtx) -> None:
    _failed_to(await sign_in(api, t, Browser(), "nobody-knows-me"), "unknown_user", 401)
    pid, subject = await add_principal(api, t)
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(text("UPDATE principals SET active = false WHERE id = :i"), {"i": pid})
    _failed_to(await sign_in(api, t, Browser(), subject), "inactive", 401)
    await _no_session(api, t)
    assert [a.outcome for a in await _attempts(api, t)] == ["unknown_user", "inactive"]


async def test_a_state_of_another_tenant_is_unknown_here(api: Api, t: TenantCtx, kms: Any) -> None:
    other = await new_api_tenant(api, kms)
    await enable_login(api, other)
    browser = Browser()
    started = await start_login(api, other, browser)
    path, query = TEST_IDP.authorize(started.headers["location"], subject=other.admin_subject)
    browser.set(host_of(api, t), LOGIN_COOKIE, login_cookie(api, other, browser) or "", "Lax")
    done = await call(api, t, browser, "GET", path, params=query, site="cross-site")
    _failed_to(done, "invalid", 400)
    await _no_session(api, t)
    assert [a.consumed_at for a in await _attempts(api, other)] == [None]  # untouched in A


# ------------------------------------------------------------------ fixation (T1) and rotation
async def test_a_planted_session_id_is_never_promoted(api: Api, t: TenantCtx) -> None:
    browser = Browser()
    planted = "A" * 43
    # planted as an attacker would: not Strict, so it rides along on the cross-site callback too
    browser.set(host_of(api, t), SESSION_COOKIE, planted, same_site="None")
    done = await sign_in(api, t, browser)
    assert done.status_code == 200
    issued = session_cookie(api, t, browser)
    assert issued is not None and issued != planted
    stale = Browser()
    stale.set(host_of(api, t), SESSION_COOKIE, planted)
    assert (await call(api, t, stale, "GET", "/v1/me")).status_code == 401


async def test_signing_in_again_revokes_the_session_the_browser_held(
    api: Api, t: TenantCtx
) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    first = session_cookie(api, t, browser)
    old = Browser()
    old.set(host_of(api, t), SESSION_COOKIE, first or "")
    await sign_in(api, t, browser)
    second = session_cookie(api, t, browser)
    assert second != first
    assert (await call(api, t, old, "GET", "/v1/me")).status_code == 401
    assert (await call(api, t, browser, "GET", "/v1/me")).status_code == 200
    row = await _session_row(api, t, first or "")
    assert row.revoke_reason == "rotated"
    created = await _audit(api, t, "session_created")
    assert created[1]["rotated_session_id"] == str(row.id)


# ------------------------------------------------------------------ re-authentication (ADR 0016 §1, §4)
async def test_reauth_without_a_session_is_a_plain_sign_in(api: Api, t: TenantCtx) -> None:
    browser = Browser()
    started = await start_login(api, t, browser, reauth=True)
    assert "prompt=login" not in started.headers["location"]
    assert (
        await finish_login(api, t, browser, started, subject=t.admin_subject)
    ).status_code == 200
    (created,) = await _audit(api, t, "session_created")
    assert created["reauth"] is False


async def test_reauth_rotates_the_session_and_asks_the_idp_for_a_fresh_login(
    api: Api, t: TenantCtx
) -> None:
    browser = Browser()
    await sign_in(api, t, browser, auth_time=int(time.time()) - 3600)
    before = session_cookie(api, t, browser)
    started = await start_login(api, t, browser, reauth=True)
    assert (
        "prompt=login" in started.headers["location"] and "max_age=0" in started.headers["location"]
    )
    done = await finish_login(api, t, browser, started, subject=t.admin_subject,
                              auth_time=int(time.time()) + 1)  # fmt: skip
    assert done.status_code == 200
    after = session_cookie(api, t, browser)
    assert after != before
    assert (await _session_row(api, t, before or "")).revoke_reason == "rotated"
    created = await _audit(api, t, "session_created")
    assert created[-1]["reauth"] is True


@pytest.mark.parametrize("case", ["other_principal", "stale_auth_time", "no_auth_time"])
async def test_a_reauth_that_does_not_prove_a_fresh_login_by_the_same_person_is_refused(
    api: Api, t: TenantCtx, case: str
) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    before = session_cookie(api, t, browser)
    _, other = await add_principal(api, t)
    started = await start_login(api, t, browser, reauth=True)
    claims: dict[str, Any] = {
        "other_principal": {"subject": other, "auth_time": int(time.time()) + 1},
        "stale_auth_time": {"subject": t.admin_subject, "auth_time": int(time.time()) - 3600},
        "no_auth_time": {"subject": t.admin_subject},
    }[case]
    if case == "no_auth_time":
        path, query = TEST_IDP.authorize(started.headers["location"], subject=t.admin_subject)
        TEST_IDP.grants[query["code"]].auth_time = None
        done = await call(api, t, browser, "GET", path, params=query, site="cross-site")
    else:
        done = await finish_login(api, t, browser, started, **claims)
    _failed_to(done, "reauth_mismatch", 403)
    # the old session is left exactly as it was
    assert session_cookie(api, t, browser) == before
    row = await _session_row(api, t, before or "")
    assert row.revoked_at is None
    assert (await call(api, t, browser, "GET", "/v1/me")).status_code == 200


# ------------------------------------------------------------------ every request (ADR 0016 §2, §3)
async def test_cookie_and_bearer_together_are_refused_before_either_is_evaluated(
    api: Api, t: TenantCtx
) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    garbage = Browser()
    garbage.set(host_of(api, t), SESSION_COOKIE, "not even well formed")
    for b, token in ((browser, t.token(api.settings)), (garbage, "garbage"), (browser, "garbage")):
        r = await call(api, t, b, "GET", "/v1/me", headers={"authorization": f"Bearer {token}"})
        assert (r.status_code, r.json()["error"]) == (400, "ambiguous_credentials")


async def test_a_cookie_of_one_tenant_is_nothing_on_another_tenants_host(
    api: Api, t: TenantCtx, kms: Any
) -> None:
    other = await new_api_tenant(api, kms)
    browser = Browser()
    await sign_in(api, t, browser)
    replay = Browser()
    replay.set(host_of(api, other), SESSION_COOKIE, session_cookie(api, t, browser) or "")
    r = await call(api, other, replay, "GET", "/v1/me")
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


async def _set(api: Api, value: str, assignments: str) -> None:
    """Move a session's clock as a DBA (the guard trigger never lets the app move time back; the
    CHECK constraints still hold)."""
    conn = await superuser(api.settings)
    try:
        await conn.execute(
            f"UPDATE sessions SET {assignments} WHERE id_hash = $1",
            hashlib.sha256(value.encode()).digest(),
        )
    finally:
        await conn.close()


async def _shift(api: Api, t: TenantCtx, value: str, column: str, interval: str) -> None:
    # the whole session moves back when its absolute expiry does (the CHECKs order the columns)
    whole = (
        "created_at",
        "authenticated_at",
        "last_seen_at",
        "idle_expires_at",
        "absolute_expires_at",
    )
    columns = {"absolute_expires_at": whole}.get(column, (column,))
    await _set(api, value, ", ".join(f"{c} = {c} - interval '{interval}'" for c in columns))


@pytest.mark.parametrize("column", ["idle_expires_at", "absolute_expires_at"])
async def test_an_expired_session_is_refused(api: Api, t: TenantCtx, column: str) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    value = session_cookie(api, t, browser) or ""
    assert (await call(api, t, browser, "GET", "/v1/me")).status_code == 200
    await _shift(api, t, value, column, "13 hours")
    assert (await call(api, t, browser, "GET", "/v1/me")).status_code == 401


async def test_the_idle_expiry_moves_at_most_once_a_minute_and_never_past_the_absolute_one(
    api: Api, t: TenantCtx
) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    value = session_cookie(api, t, browser) or ""
    first = await _session_row(api, t, value)
    await call(api, t, browser, "GET", "/v1/me")
    assert (await _session_row(api, t, value)).last_seen_at == first.last_seen_at  # < 1 minute
    await _shift(api, t, value, "last_seen_at", "2 minutes")
    await _shift(api, t, value, "idle_expires_at", "2 minutes")
    shifted = await _session_row(api, t, value)
    await call(api, t, browser, "GET", "/v1/me")
    moved = await _session_row(api, t, value)
    assert (
        moved.last_seen_at > shifted.last_seen_at
        and moved.idle_expires_at > shifted.idle_expires_at
    )
    # near the absolute expiry the idle one stops there
    await _set(
        api,
        value,
        "idle_expires_at = now() + interval '5 minutes',"
        " absolute_expires_at = now() + interval '10 minutes'",
    )
    await _shift(api, t, value, "last_seen_at", "2 minutes")
    await call(api, t, browser, "GET", "/v1/me")
    capped = await _session_row(api, t, value)
    assert capped.last_seen_at > moved.last_seen_at  # it did move...
    assert (
        capped.idle_expires_at == capped.absolute_expires_at
    )  # ...and stopped at the absolute one


async def test_a_deactivated_principal_loses_every_session_in_the_same_transaction(
    api: Api, t: TenantCtx
) -> None:
    pid, subject = await add_principal(api, t)
    one, two = Browser(), Browser()
    await sign_in(api, t, one, subject)
    await sign_in(api, t, two, subject)
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        r = await c.post(f"/v1/principals/{pid}/deactivate")
    assert r.status_code == 200, r.text
    for b in (one, two):
        assert (await call(api, t, b, "GET", "/v1/me")).status_code == 401
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        reasons = (await s.execute(text("SELECT revoke_reason FROM sessions WHERE principal_id = :p"),
                                   {"p": pid})).scalars().all()  # fmt: skip
    assert reasons == ["principal_deactivated"] * 2
    (event,) = await _audit(api, t, "principal_deactivated")
    assert len(event["sessions_revoked"]) == 2 and event["authenticated_at"]


async def test_an_inactive_principal_is_refused_even_with_a_live_session_row(
    api: Api, t: TenantCtx
) -> None:
    pid, subject = await add_principal(api, t)
    browser = Browser()
    await sign_in(api, t, browser, subject)
    async with tenant_tx(api.sessions, t.tenant_id) as s:  # bypassing the route on purpose
        await s.execute(text("UPDATE principals SET active = false WHERE id = :i"), {"i": pid})
    assert (await call(api, t, browser, "GET", "/v1/me")).status_code == 401


# ------------------------------------------------------------------ CSRF and fetch metadata (T4, T5)
async def test_state_changing_cookie_requests_need_the_token_and_the_tenant_origin(
    api: Api, t: TenantCtx, kms: Any
) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    token = await csrf_token(api, t, browser)
    other_browser = Browser()
    await sign_in(api, t, other_browser)
    others_token = await csrf_token(api, t, other_browser)
    other = await new_api_tenant(api, kms)
    body = {"name": "Acme"}

    async def post(**kw: Any) -> Any:
        return await call(api, t, browser, "POST", "/v1/clients", json=body, **kw)

    for kw in (
        {}, {"csrf": "dev.AAAA"}, {"csrf": others_token}, {"csrf": token, "origin": None},
        {"csrf": token, "origin": "https://evil.example"},
        {"csrf": token, "origin": f"https://{host_of(api, other)}"},
        {"csrf": token, "origin": f"http://{host_of(api, t)}"},  # another scheme: another origin
        {"csrf": token, "origin": "null"},
    ):  # fmt: skip
        r = await post(**kw)
        assert (r.status_code, r.json()["error"]) == (403, "csrf"), kw
    ok = await post(csrf=token)
    assert ok.status_code == 201, ok.text
    referer = await post(csrf=token, origin=None,
                         headers={"referer": f"https://{host_of(api, t)}/clients/new"})  # fmt: skip
    assert referer.status_code == 201, referer.text
    # bearer callers are not asked for a token (no ambient credential)
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        assert (await c.post("/v1/clients", json=body)).status_code == 201


async def test_the_csrf_token_is_bound_to_the_session_and_dies_with_it(
    api: Api, t: TenantCtx
) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    token = await csrf_token(api, t, browser)
    assert (await call(api, t, browser, "GET", "/v1/auth/csrf")).headers[
        "cache-control"
    ] == "no-store"
    await sign_in(api, t, browser)  # a new session id: the old token means nothing
    r = await call(api, t, browser, "POST", "/v1/clients", json={"name": "A"}, csrf=token)
    assert (r.status_code, r.json()["error"]) == (403, "csrf")


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_cookie_requests_from_another_site_or_a_sibling_subdomain_are_refused(
    api: Api, t: TenantCtx, site: str
) -> None:
    """Content routes are GETs with side effects (audit, anchor): even a GET is refused."""
    browser = Browser()
    await sign_in(api, t, browser)
    browser_all = Browser(jar=browser.jar)  # send the Strict cookie anyway: the server must refuse
    r = await call(api, t, browser_all, "GET", "/v1/me", site=None,
                   headers={"sec-fetch-site": site})  # fmt: skip
    assert (r.status_code, r.json()["error"]) == (403, "cross_site")
    for allowed in ("same-origin", "none", None):
        assert (await call(api, t, browser, "GET", "/v1/me", site=allowed)).status_code == 200
    async with api.client(t.subdomain, t.token(api.settings)) as c:  # bearer: unaffected
        assert (await c.get("/v1/me", headers={"sec-fetch-site": "cross-site"})).status_code == 200


async def test_the_csrf_route_and_logout_are_for_sessions(api: Api, t: TenantCtx) -> None:
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        assert (await c.get("/v1/auth/csrf")).json()["error"] == "not_a_session"
        assert (await c.post("/v1/auth/logout")).json()["error"] == "not_a_session"


# ------------------------------------------------------------------ logout and revocation (T7)
async def test_logout_revokes_the_session_and_clears_the_cookie(api: Api, t: TenantCtx) -> None:
    browser = Browser()
    await sign_in(api, t, browser)
    value = session_cookie(api, t, browser) or ""
    refused = await call(api, t, browser, "POST", "/v1/auth/logout")  # no CSRF token
    assert refused.status_code == 403
    token = await csrf_token(api, t, browser)
    r = await call(api, t, browser, "POST", "/v1/auth/logout", csrf=token)
    assert r.status_code == 204
    cleared = [h for h in browser.seen if h.startswith(f"{SESSION_COOKIE}=;")]
    assert cleared and "Max-Age=0" in cleared[-1]
    stale = Browser()
    stale.set(host_of(api, t), SESSION_COOKIE, value)
    assert (await call(api, t, stale, "GET", "/v1/me")).status_code == 401
    assert (await _session_row(api, t, value)).revoke_reason == "logout"
    (event,) = await _audit(api, t, "session_revoked")
    assert event["reason"] == "logout"


# ------------------------------------------------------------------ the caller's own sessions
async def test_the_caller_lists_their_own_sessions_and_ends_them(api: Api, t: TenantCtx) -> None:
    here, there = Browser(), Browser()
    await sign_in(api, t, here)
    await sign_in(api, t, there)
    _, other_subject = await add_principal(api, t)
    stranger = Browser()
    await sign_in(api, t, stranger, other_subject)
    page = (await call(api, t, here, "GET", "/v1/me/sessions")).json()
    assert [s["current"] for s in page["items"]] == [True, False]
    assert all(s["live"] for s in page["items"])
    assert "id_hash" not in page["items"][0]
    current, other = page["items"][0]["id"], page["items"][1]["id"]
    strangers = (await call(api, t, stranger, "GET", "/v1/me/sessions")).json()["items"][0]["id"]
    token = await csrf_token(api, t, here)
    r = await call(api, t, here, "POST", f"/v1/me/sessions/{strangers}/revoke", csrf=token)
    assert r.status_code == 404  # another principal's session does not exist for the caller
    r = await call(api, t, here, "POST", f"/v1/me/sessions/{other}/revoke", csrf=token)
    assert r.status_code == 200 and r.json()["revoke_reason"] == "self" and not r.json()["live"]
    assert (await call(api, t, there, "GET", "/v1/me")).status_code == 401
    r = await call(api, t, here, "POST", f"/v1/me/sessions/{other}/revoke", csrf=token)
    assert (r.status_code, r.json()["error"]) == (409, "already_revoked")
    r = await call(api, t, here, "POST", f"/v1/me/sessions/{current}/revoke", csrf=token)
    assert r.status_code == 200
    assert (await call(api, t, here, "GET", "/v1/me")).status_code == 401


async def test_ending_the_current_session_works_without_a_recent_sign_in_but_others_need_one(
    api: Api, t: TenantCtx
) -> None:
    here, there = Browser(), Browser()
    await sign_in(api, t, here, auth_time=int(time.time()) - 3600)
    await sign_in(api, t, there)
    page = (await call(api, t, here, "GET", "/v1/me/sessions")).json()["items"]
    current = next(s["id"] for s in page if s["current"])
    other = next(s["id"] for s in page if not s["current"])
    token = await csrf_token(api, t, here)
    r = await call(api, t, here, "POST", f"/v1/me/sessions/{other}/revoke", csrf=token)
    assert (r.status_code, r.json()["error"]) == (401, "reauth_required")
    assert (await call(api, t, there, "GET", "/v1/me")).status_code == 200  # untouched
    r = await call(api, t, here, "POST", f"/v1/me/sessions/{current}/revoke", csrf=token)
    assert r.status_code == 200


async def test_a_tenant_admin_ends_every_session_of_a_principal(api: Api, t: TenantCtx) -> None:
    pid, subject = await add_principal(api, t)
    one, two = Browser(), Browser()
    await sign_in(api, t, one, subject)
    await sign_in(api, t, two, subject)
    _, reviewer = await add_principal(api, t, roles=[("reviewer", "tenant", None)])
    async with api.client(t.subdomain, t.token(api.settings, subject=reviewer)) as c:
        assert (await c.post(f"/v1/principals/{pid}/sessions/revoke")).status_code == 403
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        r = await c.post(f"/v1/principals/{pid}/sessions/revoke")
        assert r.status_code == 200 and len(r.json()["revoked"]) == 2
        assert (await c.post(f"/v1/principals/{uuid.uuid4()}/sessions/revoke")).status_code == 404
    for b in (one, two):
        assert (await call(api, t, b, "GET", "/v1/me")).status_code == 401
    (event,) = await _audit(api, t, "sessions_revoked")
    assert event["reason"] == "admin" and len(event["sessions_revoked"]) == 2


# ------------------------------------------------------------------ recent sign-in (ADR 0016 §4)
async def _sensitive_requests(api: Api, t: TenantCtx) -> list[tuple[str, str, Any]]:
    """Every action ADR 0016 §4 covers, against objects that exist (so authorization passes)."""
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        client = (await c.post("/v1/clients", json={"name": "C"})).json()["id"]
        closed_client = (await c.post("/v1/clients", json={"name": "D"})).json()["id"]
        until = "2099-01-01T00:00:00+00:00"
        matter = (await c.post(f"/v1/clients/{client}/matters",
                               json={"name": "M", "retention_until": until})).json()["id"]  # fmt: skip
        closed_matter = (await c.post(f"/v1/clients/{client}/matters",
                                      json={"name": "N", "retention_until": until})).json()["id"]  # fmt: skip
        assert (await c.post(f"/v1/matters/{closed_matter}/close")).status_code == 200
        assert (await c.post(f"/v1/clients/{closed_client}/close")).status_code == 200
        group = (await c.post("/v1/groups", json={"name": "G"})).json()["id"]
        pid, _ = await add_principal(api, t)
        assert (
            await c.post(f"/v1/groups/{group}/members", json={"principal_id": str(pid)})
        ).status_code == 204
        assignment = (await c.post("/v1/role-assignments", json={
            "principal_id": str(pid), "role": "reviewer", "scope_type": "tenant"})).json()["id"]  # fmt: skip
        sp = spec()
        conn = (await c.post(f"/v1/clients/{client}/connections", json={
            "source": "dummy", "external_org_id": sp.workspace_id,
            "config": {"spec": sp.model_dump(mode="json"), "epoch": 0},
            "credentials": {"access_token": secret()}})).json()  # fmt: skip
    job = new_job = uuid.uuid4()
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(
            text("INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status,"
                 " connector_version, requested_by) VALUES (:i, :t, :m, :c, 'pending', '0', 'tests')"),
            {"i": new_job, "t": t.tenant_id, "m": matter, "c": conn["id"]},
        )  # fmt: skip
    _, subject = await add_principal(api, t)
    return [
        ("POST", f"/v1/jobs/{new_job}/finish-without-access", {"reason": "r"}),
        ("POST", f"/v1/matters/{matter}/close", None),
        ("POST", f"/v1/clients/{client}/close", None),
        ("POST", f"/v1/matters/{closed_matter}/reopen", None),
        ("POST", f"/v1/clients/{closed_client}/reopen", None),
        ("POST", f"/v1/clients/{client}/exports",
         {"size_bytes": 1000, "limits": {"max_entries": 10}}),
        ("POST", "/v1/role-assignments",
         {"principal_id": str(pid), "role": "auditor", "scope_type": "tenant"}),
        ("POST", f"/v1/role-assignments/{assignment}/revoke", None),
        ("POST", f"/v1/groups/{group}/members", {"principal_id": str(t.admin_id)}),
        ("POST", f"/v1/groups/{group}/members/{pid}/remove", None),
        ("POST", f"/v1/principals/{pid}/deactivate", None),
        ("POST", f"/v1/principals/{pid}/sessions/revoke", None),
        ("POST", f"/v1/jobs/{job}/renders", {}),
        ("POST", f"/v1/jobs/{job}/reports", {"reason": "needed for court"}),
    ]  # fmt: skip


@pytest.mark.parametrize("how", ["bearer_auth_time", "bearer_iat", "session"])
async def test_every_sensitive_action_needs_a_recent_sign_in(
    api: Api, t: TenantCtx, how: str
) -> None:
    requests = await _sensitive_requests(api, t)
    hour_ago = int(time.time()) - 3600
    browser = Browser()
    if how == "session":
        await sign_in(api, t, browser, auth_time=hour_ago)
        token = await csrf_token(api, t, browser)
    before = len(await _all_audit(api, t))
    for method, path, body in requests:
        if how == "session":
            r = await call(api, t, browser, method, path, json=body, csrf=token)
        else:
            claims = {"auth_time": hour_ago} if how == "bearer_auth_time" else {"iat": hour_ago}
            async with api.client(t.subdomain, t.token(api.settings, **claims)) as c:
                r = await c.request(method, path, json=body)
        assert (r.status_code, r.json().get("error")) == (401, "reauth_required"), (path, r.text)
    assert len(await _all_audit(api, t)) == before  # nothing was done, nothing audited


async def test_a_recent_sign_in_passes_and_the_audit_records_when_it_was(
    api: Api, t: TenantCtx
) -> None:
    signed_in = int(time.time()) - 5
    async with api.client(t.subdomain, t.token(api.settings, auth_time=signed_in)) as c:
        client = (await c.post("/v1/clients", json={"name": "C"})).json()["id"]
        assert (await c.post(f"/v1/clients/{client}/close")).status_code == 200
    (event,) = await _audit(api, t, "client_closed")
    assert event["authenticated_at"].startswith(
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(signed_in))
    )


async def _all_audit(api: Api, t: TenantCtx) -> list[Any]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return list((await s.execute(text("SELECT seq FROM custody_events WHERE stream_id = :t"),
                                     {"t": t.tenant_id})).scalars())  # fmt: skip


# ------------------------------------------------------------------ throttling
async def test_sign_in_starts_are_capped_per_address(api: Api, t: TenantCtx) -> None:
    api.settings = api.settings.model_copy(update={"api_login_starts_per_window": 3})
    api.resources.settings = api.settings
    browser = Browser()
    statuses = [(await start_login(api, t, browser)).status_code for _ in range(5)]
    assert statuses == [303, 303, 303, 429, 429]
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        assert (await s.execute(text("SELECT count(*) FROM login_attempts"))).scalar_one() == 3


async def test_failed_callbacks_count_as_authentication_failures(api: Api, t: TenantCtx) -> None:
    api.settings = api.settings.model_copy(update={"api_auth_failures_per_minute": 3})
    api.resources.settings = api.settings
    browser = Browser()
    for _ in range(3):
        r = await call(api, t, browser, "GET", "/v1/auth/callback", site="cross-site",
                       params={"state": "B" * 43, "code": "c"})  # fmt: skip
        assert r.status_code == 400
    r = await call(api, t, browser, "GET", "/v1/auth/callback", site="cross-site",
                   params={"state": "B" * 43, "code": "c"})  # fmt: skip
    assert r.status_code == 429
    assert (await start_login(api, t, browser)).status_code == 429


# ------------------------------------------------------------------ crash points (plan §8)
class CrashError(Exception):
    """A process dying at that point: the request ends there (the outermost middleware's 500)."""


@pytest.mark.parametrize("point", ["consumed", "session_tx", "session_committed"])
async def test_a_crash_during_the_callback(
    api: Api, t: TenantCtx, point: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import edisc_api.routes.auth as auth_routes

    browser = Browser()
    await sign_in(api, t, browser)
    old = session_cookie(api, t, browser) or ""

    async def crash(at: str) -> None:
        if at == point:
            raise CrashError(at)

    monkeypatch.setattr(auth_routes, "crash_point", crash)
    started = await start_login(api, t, browser)
    done = await finish_login(api, t, browser, started, subject=t.admin_subject)
    assert (
        done.status_code == 500 and done.json()["error"] == "internal_error"
    )  # no page, no cookie
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        attempt = (
            await s.execute(text("SELECT * FROM login_attempts ORDER BY started_at DESC LIMIT 1"))
        ).one()
        sessions = (
            (await s.execute(text("SELECT revoke_reason FROM sessions ORDER BY created_at")))
            .scalars()
            .all()
        )
    assert attempt.consumed_at is not None  # single use, whatever happened next
    if point == "session_committed":
        # a session no browser holds: it idles out; the old one was rotated with it
        assert attempt.outcome == "completed" and sessions == ["rotated", None]
    else:
        # both or neither: no new session, the old one untouched, no outcome
        assert attempt.outcome is None and sessions == [None]
        assert (await call(api, t, browser, "GET", "/v1/me")).status_code == 200
    assert session_cookie(api, t, browser) == old  # the browser never got a new cookie
    monkeypatch.setattr(auth_routes, "crash_point", auth_routes._no_crash)
    assert (await sign_in(api, t, Browser())).status_code == 200  # signing in again works
