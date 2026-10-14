"""A test OIDC provider and a browser stand-in for the sign-in tests (ADR 0016; M17 step 2).

``TestIdp`` answers the API's server-side calls (discovery, the code exchange) through an httpx
transport mounted for ``https://dev-idp.edisc.localhost`` (the dev issuer, signing with the dev key:
``jwks_url = "dev:"``). The browser leg (``/authorize``) is :meth:`TestIdp.authorize`, which the
test calls with the URL the API redirected to; it never authenticates anyone by itself: the test
says who signs in and when. Knobs make it answer wrongly (nonce, audience, status, issuer).

``Browser`` keeps cookies per host the way the tests need (``__Host-`` cookies are host-only;
Secure is ignored because the test transport is http). SameSite is browser behaviour: the
Playwright test checks it in real browsers.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx

from edisc_api.auth import DEV_ISSUER, dev_token
from edisc_core.settings import Settings

IDP_HOST = urllib.parse.urlsplit(DEV_ISSUER).netloc
LOGIN_CLIENT = "edisc-browser"


@dataclass
class _Grant:
    client_id: str
    redirect_uri: str
    challenge: str
    nonce: str
    subject: str
    auth_time: int | None
    claims: dict[str, Any]


@dataclass
class TestIdp:
    __test__ = False  # not a pytest class

    settings: Settings | None = None
    grants: dict[str, _Grant] = field(default_factory=dict)
    token_calls: int = 0
    discovery_issuer: str = DEV_ISSUER
    token_status: int = 200
    # where browsers are sent (another SITE than the tenant hosts in the real-browser test)
    authorize_base: str = DEV_ISSUER

    def reset(self) -> None:
        self.grants.clear()
        self.token_calls = 0
        self.discovery_issuer = DEV_ISSUER
        self.token_status = 200
        self.authorize_base = DEV_ISSUER

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def authorize(
        self, url: str, *, subject: str, auth_time: int | None = None, **claims: Any
    ) -> tuple[str, dict[str, str]]:
        """The browser at the IdP: returns (callback path, query) the IdP redirects back with."""
        parts = urllib.parse.urlsplit(url)
        assert (
            f"{parts.scheme}://{parts.netloc}" == self.authorize_base and parts.path == "/authorize"
        )
        q = dict(urllib.parse.parse_qsl(parts.query))
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        assert q["scope"] == "openid"
        code = secrets.token_urlsafe(24)
        self.grants[code] = _Grant(
            q["client_id"], q["redirect_uri"], q["code_challenge"], q["nonce"], subject,
            int(time.time()) if auth_time is None else auth_time, claims,
        )  # fmt: skip
        back = urllib.parse.urlsplit(q["redirect_uri"])
        return back.path, {"code": code, "state": q["state"]}

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.discovery_issuer,
                "authorization_endpoint": f"{self.authorize_base}/authorize",
                "token_endpoint": f"{DEV_ISSUER}/token",
                "jwks_uri": f"{DEV_ISSUER}/jwks",
            })  # fmt: skip
        if request.url.path == "/token" and request.method == "POST":
            self.token_calls += 1
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            grant = self.grants.pop(form.get("code", ""), None)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "server_error"})
            if grant is None or form.get("grant_type") != "authorization_code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                challenge != grant.challenge
                or form.get("client_id") != grant.client_id
                or form.get("redirect_uri") != grant.redirect_uri
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert self.settings is not None
            claims: dict[str, Any] = {"nonce": grant.nonce, **grant.claims}
            if grant.auth_time is not None:
                claims["auth_time"] = grant.auth_time
            audience = claims.pop("aud", grant.client_id)
            issuer = claims.pop("iss", DEV_ISSUER)
            token = dev_token(
                self.settings, subject=grant.subject, audience=audience, issuer=issuer, **claims
            )
            return httpx.Response(200, json={"id_token": token, "access_token": "unused",
                                             "token_type": "Bearer"})  # fmt: skip
        return httpx.Response(404)


TEST_IDP = TestIdp()


def parse_set_cookie(header: str) -> tuple[str, str, dict[str, str]]:
    first, *attrs = [p.strip() for p in header.split(";")]
    name, _, value = first.partition("=")
    flags = {}
    for attr in attrs:
        k, _, v = attr.partition("=")
        flags[k.lower()] = v
    return name, value, flags


@dataclass
class Browser:
    """Cookies per host (with their SameSite), the Sec-Fetch-Site a page sends, nothing else. On a
    cross-site navigation (the IdP's redirect back) Strict cookies are not sent, as in a browser."""

    jar: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)  # every Set-Cookie header received

    def cookie_header(self, host: str, *, cross_site: bool = False) -> str | None:
        cookies = {
            k: v for k, (v, same_site) in self.jar.get(host, {}).items()
            if not (cross_site and same_site.lower() == "strict")
        }  # fmt: skip
        return "; ".join(f"{k}={v}" for k, v in cookies.items()) if cookies else None

    def absorb(self, host: str, response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            self.seen.append(header)
            name, value, flags = parse_set_cookie(header)
            jar = self.jar.setdefault(host, {})
            if flags.get("max-age") == "0":
                jar.pop(name, None)
            else:
                jar[name] = (value, flags.get("samesite", "Lax"))

    def set(self, host: str, name: str, value: str, same_site: str = "Strict") -> None:
        self.jar.setdefault(host, {})[name] = (value, same_site)

    def get_cookie(self, host: str, name: str) -> str | None:
        entry = self.jar.get(host, {}).get(name)
        return entry[0] if entry else None

    def drop(self, host: str, name: str) -> None:
        self.jar.get(host, {}).pop(name, None)
