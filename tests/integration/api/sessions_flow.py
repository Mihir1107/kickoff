"""Helpers for the browser-session tests: enable sign-in on a tenant, drive the login through the
test IdP, call the API with the session cookie (ADR 0016; M17 step 2)."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import text

from edisc_api.sessions import LOGIN_COOKIE, SESSION_COOKIE
from edisc_db.session import tenant_tx

from .conftest import Api, TenantCtx
from .oidc import LOGIN_CLIENT, TEST_IDP, Browser


def host_of(api: Api, t: TenantCtx) -> str:
    return f"{t.subdomain}.{api.settings.api_base_domain}"


def origin_of(api: Api, t: TenantCtx) -> str:
    return f"{api.settings.api_public_scheme}://{host_of(api, t)}"


async def enable_login(api: Api, t: TenantCtx) -> None:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(text("UPDATE tenant_idps SET login_client_id = :c"), {"c": LOGIN_CLIENT})


async def call(
    api: Api, t: TenantCtx, browser: Browser, method: str, path: str, *,
    params: dict[str, str] | None = None, headers: dict[str, str] | None = None,
    json: Any = None, site: str | None = "same-origin", csrf: str | None = None,
    origin: str | None = "",
) -> httpx.Response:  # fmt: skip
    """A request the tenant's page (or a navigation) makes. ``csrf``: the X-CSRF-Token;
    ``origin``: "" = this tenant's origin, None = no Origin header."""
    host = host_of(api, t)
    h: dict[str, str] = {}
    if site is not None:
        h["sec-fetch-site"] = site
    if method not in ("GET", "HEAD"):
        if origin == "":
            h["origin"] = origin_of(api, t)
        elif origin is not None:
            h["origin"] = origin
    if csrf is not None:
        h["x-csrf-token"] = csrf
    cookie = browser.cookie_header(host, cross_site=site == "cross-site")
    if cookie:
        h["cookie"] = cookie
    h.update(headers or {})
    async with api.client(t.subdomain) as c:
        r = await c.request(method, path, params=params, headers=h, json=json)
    browser.absorb(host, r)
    return r


async def start_login(
    api: Api, t: TenantCtx, browser: Browser, *, return_to: str | None = None,
    reauth: bool = False,
) -> httpx.Response:  # fmt: skip
    params = {} if return_to is None else {"return_to": return_to}
    if reauth:
        params["reauth"] = "1"
    return await call(api, t, browser, "GET", "/v1/auth/login", params=params, site="same-origin")


async def finish_login(
    api: Api, t: TenantCtx, browser: Browser, started: httpx.Response, *, subject: str,
    auth_time: int | None = None, **claims: Any,
) -> httpx.Response:  # fmt: skip
    assert started.status_code == 303, started.text
    path, query = TEST_IDP.authorize(
        started.headers["location"], subject=subject, auth_time=auth_time, **claims
    )
    # the IdP's redirect back is a cross-site navigation
    return await call(api, t, browser, "GET", path, params=query, site="cross-site")


async def sign_in(
    api: Api, t: TenantCtx, browser: Browser, subject: str | None = None, *,
    return_to: str | None = None, reauth: bool = False, auth_time: int | None = None,
    **claims: Any,
) -> httpx.Response:  # fmt: skip
    started = await start_login(api, t, browser, return_to=return_to, reauth=reauth)
    return await finish_login(
        api, t, browser, started, subject=subject or t.admin_subject, auth_time=auth_time, **claims
    )


async def csrf_token(api: Api, t: TenantCtx, browser: Browser) -> str:
    r = await call(api, t, browser, "GET", "/v1/auth/csrf")
    assert r.status_code == 200, r.text
    token: str = r.json()["csrf_token"]
    return token


def session_cookie(api: Api, t: TenantCtx, browser: Browser) -> str | None:
    return browser.get_cookie(host_of(api, t), SESSION_COOKIE)


def login_cookie(api: Api, t: TenantCtx, browser: Browser) -> str | None:
    return browser.get_cookie(host_of(api, t), LOGIN_COOKIE)
