"""OIDC relying party for browser sign-in (ADR 0016 §1; M17 step 2).

Authorization code + PKCE (S256) with the tenant's ONE sign-in IdP (``tenant_idps.login_client_id``).
- The discovery document is fetched from the REGISTERED issuer (``<issuer>/.well-known/openid-
  configuration``), https, redirects not followed, cached; its ``issuer`` must equal the registered
  one and its endpoints must be https.
- The ID token is verified with the REGISTERED JWKS (``tenant_idps.jwks_url``, the same cache the
  bearer path uses), never a key the discovery document or the token points at: signature, algorithm
  allow-list, ``iss``, ``aud`` (our client id; ``azp`` when several audiences), ``exp``, ``iat``,
  ``nonce``. ``auth_time`` is read when present (re-authentication requires it).
- The IdP's tokens are used once, inside the callback request, and never stored or returned.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt

from edisc_api.auth import ALLOWED_ALGS, JwksCache
from edisc_core.settings import Settings

DISCOVERY_SECONDS = 3600


class OidcError(Exception):
    """The IdP answered something we do not accept. ``code`` is from the fixed list of ADR 0016 §1."""

    def __init__(self, code: str, why: str) -> None:
        super().__init__(why)
        self.code = code


@dataclass(frozen=True)
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str


@dataclass(frozen=True)
class IdToken:
    subject: str
    groups: list[str]
    auth_time: datetime | None
    issued_at: datetime


def _https(url: object) -> str:
    if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme != "https":
        raise OidcError("invalid", "discovery endpoints must be https")
    return url


@dataclass
class OidcClient:
    settings: Settings
    http: httpx.AsyncClient
    jwks: JwksCache
    _cache: dict[str, tuple[Provider, float]] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def provider(self, issuer: str) -> Provider:
        cached = self._cache.get(issuer)
        if cached is not None and time.monotonic() - cached[1] < DISCOVERY_SECONDS:
            return cached[0]
        async with self._locks.setdefault(issuer, asyncio.Lock()):
            url = _https(issuer.rstrip("/") + "/.well-known/openid-configuration")
            try:
                response = await self.http.get(url, timeout=5, follow_redirects=False)
                response.raise_for_status()
                doc = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise OidcError("invalid", f"discovery failed: {type(exc).__name__}") from exc
            if not isinstance(doc, dict) or doc.get("issuer") != issuer:
                raise OidcError("invalid", "discovery issuer differs from the registered one")
            provider = Provider(
                issuer, _https(doc.get("authorization_endpoint")), _https(doc.get("token_endpoint"))
            )
            self._cache[issuer] = (provider, time.monotonic())
            return provider

    @staticmethod
    def authorize_url(
        provider: Provider,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        nonce: str,
        challenge: str,
        reauth: bool,
    ) -> str:
        query = {
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
            "scope": "openid", "state": state, "nonce": nonce, "code_challenge": challenge,
            "code_challenge_method": "S256",
            **({"prompt": "login", "max_age": "0"} if reauth else {}),
        }  # fmt: skip
        sep = "&" if urllib.parse.urlsplit(provider.authorization_endpoint).query else "?"
        return provider.authorization_endpoint + sep + urllib.parse.urlencode(query)

    async def exchange(
        self, provider: Provider, *, client_id: str, redirect_uri: str, code: str, verifier: str
    ) -> str:
        """The ID token of the code exchange (a public client: PKCE binds the code to us)."""
        try:
            response = await self.http.post(
                provider.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
                timeout=10,
                follow_redirects=False,
            )
            body = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("invalid", f"code exchange failed: {type(exc).__name__}") from exc
        if response.status_code != 200 or not isinstance(body, dict):
            raise OidcError("invalid", f"code exchange answered {response.status_code}")
        token = body.get("id_token")
        if not isinstance(token, str):
            raise OidcError("invalid", "no id_token in the exchange answer")
        return token

    async def verify(
        self, token: str, *, issuer: str, jwks_url: str, client_id: str, nonce: str,
        groups_claim: str,
    ) -> IdToken:  # fmt: skip
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcError("invalid", "malformed id_token") from exc
        if header.get("alg") not in ALLOWED_ALGS:
            raise OidcError("invalid", "id_token algorithm not allowed")
        key = await self.jwks.key(jwks_url, header.get("kid"))
        try:
            claims: dict[str, Any] = jwt.decode(
                token, key, algorithms=list(ALLOWED_ALGS), audience=client_id, issuer=issuer,
                leeway=self.settings.api_jwt_leeway_seconds,
                options={"require": ["exp", "iat", "sub", "iss", "aud", "nonce"]},
            )  # fmt: skip
        except jwt.PyJWTError as exc:
            raise OidcError("invalid", f"id_token rejected: {type(exc).__name__}") from exc
        aud = claims["aud"]
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != client_id:
            raise OidcError("invalid", "id_token azp is not our client")
        if not isinstance(claims["nonce"], str) or claims["nonce"] != nonce:
            raise OidcError("invalid", "id_token nonce mismatch")
        iat = claims["iat"]
        if (
            not isinstance(iat, int | float)
            or iat > time.time() + self.settings.api_jwt_leeway_seconds
        ):
            raise OidcError("invalid", "id_token issued in the future")
        auth_time = claims.get("auth_time")
        groups = claims.get(groups_claim) or []
        return IdToken(
            subject=str(claims["sub"]),
            groups=[str(g) for g in groups] if isinstance(groups, list) else [],
            auth_time=(
                datetime.fromtimestamp(auth_time, UTC)
                if isinstance(auth_time, int | float) and not isinstance(auth_time, bool)
                else None
            ),
            issued_at=datetime.fromtimestamp(iat, UTC),
        )
