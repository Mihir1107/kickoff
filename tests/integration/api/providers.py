"""Fake Slack and Microsoft identity endpoints for the install flows (M17 step 6).

Mounted on the API's HTTP client by host (``slack.com``, ``login.microsoftonline.com``), so the
product calls the REAL production URLs. They behave like the providers where it matters to the
attacks the tests make:

- **Slack** ``oauth.v2.access`` checks our client id and secret, the single-use code and the
  ``redirect_uri`` it was issued for, and answers the team (and enterprise) the CODE was issued
  in: the authenticated answer, which the redirect's query cannot change.
- **Microsoft** ``/organizations/oauth2/v2.0/token`` checks PKCE, the redirect URI and our client
  assertion (signed by the platform key, verified with our published JWKS), and answers an ID
  token SIGNED by its own key with ``tid`` = the Entra tenant the code was issued in, the nonce and
  ``wids`` (only when asked for: Entra emits it only when the app registration's optional claims
  include it); ``/<tid>/oauth2/v2.0/token`` (client credentials) answers a token (``tid``,
  ``appid``, ``roles`` = what that tenant's administrator granted) only for a tenant that
  consented, like Entra (no service principal there: ``unauthorized_client``). ``/common/discovery/v2.0/keys`` publishes the signing key.

Every token handed out is kept in ``issued`` so tests can scan responses and logs for them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

SLACK_HOST = "slack.com"
MS_HOST = "login.microsoftonline.com"
MS_KID = "fake-ms-key"


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


@dataclass
class _SlackCode:
    redirect_uri: str
    team: str
    enterprise: str | None
    scopes: tuple[str, ...]


@dataclass
class FakeSlack:
    client_id: str = ""
    client_secret: str = ""
    codes: dict[str, _SlackCode] = field(default_factory=dict)
    issued: list[str] = field(default_factory=list)
    exchanges: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def authorize(self, url: str, *, team: str, enterprise: str | None = None,
                  scopes: tuple[str, ...] | None = None, deny: bool = False) -> dict[str, str]:  # fmt: skip
        """The admin at Slack approves (or denies): the query Slack redirects back with."""
        parts = urllib.parse.urlsplit(url)
        assert (parts.scheme, parts.netloc, parts.path) == (
            "https",
            SLACK_HOST,
            "/oauth/v2/authorize",
        )
        q = dict(urllib.parse.parse_qsl(parts.query))
        assert q["client_id"] == self.client_id
        if deny:
            return {"error": "access_denied", "state": q["state"]}
        code = secrets.token_urlsafe(24)
        asked = tuple(q["scope"].split(","))
        self.codes[code] = _SlackCode(q["redirect_uri"], team, enterprise,
                                      asked if scopes is None else scopes)  # fmt: skip
        return {"code": code, "state": q["state"]}

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/oauth.v2.access" or request.method != "POST":
            return httpx.Response(404)
        self.exchanges += 1
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        if (form.get("client_id"), form.get("client_secret")) != (
            self.client_id,
            self.client_secret,
        ):
            return httpx.Response(200, json={"ok": False, "error": "invalid_client_id"})
        grant = self.codes.pop(form.get("code", ""), None)
        if grant is None or form.get("redirect_uri") != grant.redirect_uri:
            return httpx.Response(200, json={"ok": False, "error": "invalid_code"})
        token = f"xoxb-{secrets.token_hex(12)}"
        self.issued.append(token)
        return httpx.Response(200, json={
            "ok": True, "access_token": token, "token_type": "bot",
            "scope": ",".join(grant.scopes), "bot_user_id": "U0BOT",
            "app_id": "A0APP", "team": {"id": grant.team, "name": "Team"},
            "enterprise": {"id": grant.enterprise, "name": "Org"} if grant.enterprise else None,
            "is_enterprise_install": False,
        })  # fmt: skip


@dataclass
class _MsCode:
    redirect_uri: str
    challenge: str
    nonce: str
    tid: str
    wids: object  # None: no wids claim at all


@dataclass
class FakeMicrosoft:
    client_id: str = ""
    client_jwks: dict[str, Any] | None = None  # our published JWKS (client assertions checked)
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    codes: dict[str, _MsCode] = field(default_factory=dict)
    granted: dict[str, list[str]] = field(default_factory=dict)  # tid -> app roles granted
    # tid -> claims that replace the consent token's own (a token of another tenant or app)
    consent_claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    issued: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def authorize(self, url: str, *, tid: str, admin: bool | None = True,
                  roles: list[str] | None = None, wids: object = None) -> dict[str, str]:  # fmt: skip
        """A user of ``tid`` signs in through the consent prompt: the query Microsoft redirects
        back with (it would also carry ``tenant`` and ``admin_consent``, which the product must
        never read). ``admin``: True/False put an administrator / another role in ``wids``; None
        leaves ``wids`` out (``wids`` given: exactly that claim). ``roles``: what the tenant's
        administrator granted (None: nobody consented, no client-credentials token)."""
        parts = urllib.parse.urlsplit(url)
        assert (parts.netloc, parts.path) == (MS_HOST, "/organizations/oauth2/v2.0/authorize")
        q = dict(urllib.parse.parse_qsl(parts.query))
        assert q["client_id"] == self.client_id and q["prompt"] == "consent"
        assert q["code_challenge_method"] == "S256" and "openid" in q["scope"].split()
        code = secrets.token_urlsafe(24)
        if wids is None and admin is not None:
            wids = (
                ["62e90394-69f5-4237-9190-012177145e10"]
                if admin
                else ["b79fbf4d-3ef9-4689-8143-76b194e85509"]
            )
        self.codes[code] = _MsCode(q["redirect_uri"], q["code_challenge"], q["nonce"], tid, wids)
        if roles is not None:
            self.granted[tid] = roles
        return {"code": code, "state": q["state"], "tenant": tid, "admin_consent": "True",
                "session_state": "x"}  # fmt: skip

    def jwks(self) -> dict[str, Any]:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**public, "kid": MS_KID, "use": "sig", "alg": "RS256"}]}

    def _assertion_ok(self, form: dict[str, str], audience: str) -> bool:
        assertion = form.get("client_assertion")
        if not assertion or self.client_jwks is None:
            self.refusals.append("no client assertion")
            return False
        kid = jwt.get_unverified_header(assertion).get("kid")
        found = [k for k in self.client_jwks["keys"] if k.get("kid") == kid]
        if not found:
            self.refusals.append("unknown client key")
            return False
        try:
            jwt.decode(assertion, jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(found[0])),
                       algorithms=["RS256"], audience=audience, issuer=self.client_id)  # fmt: skip
        except jwt.PyJWTError as exc:
            self.refusals.append(f"assertion: {type(exc).__name__}")
            return False
        return True

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/common/discovery/v2.0/keys":
            return httpx.Response(200, json=self.jwks())
        if request.method != "POST" or not path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(404)
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        audience = f"https://{MS_HOST}{path}"
        if form.get("client_id") != self.client_id or not self._assertion_ok(form, audience):
            return httpx.Response(401, json={"error": "invalid_client"})
        now = int(time.time())
        if path == "/organizations/oauth2/v2.0/token":
            grant = self.codes.pop(form.get("code", ""), None)
            if (
                grant is None or form.get("grant_type") != "authorization_code"
                or form.get("redirect_uri") != grant.redirect_uri
                or _challenge(form.get("code_verifier", "")) != grant.challenge
            ):  # fmt: skip
                return httpx.Response(400, json={"error": "invalid_grant"})
            id_token = jwt.encode({
                "iss": f"https://{MS_HOST}/{grant.tid}/v2.0", "aud": self.client_id,
                "tid": grant.tid, "nonce": grant.nonce, "sub": "admin-sub",
                "iat": now, "nbf": now, "exp": now + 3600,
                **({} if grant.wids is None else {"wids": grant.wids}),
            }, self.key, algorithm="RS256", headers={"kid": MS_KID})  # fmt: skip
            access = f"eyJ.{secrets.token_hex(8)}.sig"
            self.issued += [id_token, access]
            return httpx.Response(200, json={"id_token": id_token, "access_token": access,
                                             "token_type": "Bearer"})  # fmt: skip
        tid = path.split("/")[1]
        if form.get("grant_type") != "client_credentials":
            return httpx.Response(400, json={"error": "invalid_grant"})
        if tid not in self.granted:  # no consent in that tenant: no service principal there
            return httpx.Response(400, json={"error": "unauthorized_client"})
        claims = {"tid": tid, "appid": self.client_id, "roles": self.granted[tid],
                  "aud": "https://graph.microsoft.com"} | self.consent_claims.get(tid, {})  # fmt: skip
        body = base64.urlsafe_b64encode(json.dumps(
            {k: v for k, v in claims.items() if v is not None}
        ).encode()).rstrip(b"=").decode()  # fmt: skip
        token = f"eyJhbGciOiJSUzI1NiJ9.{body}.c2ln"
        self.issued.append(token)
        return httpx.Response(200, json={"access_token": token, "token_type": "Bearer",
                                         "expires_in": 3599})  # fmt: skip


FAKE_SLACK = FakeSlack()
FAKE_MICROSOFT = FakeMicrosoft()
