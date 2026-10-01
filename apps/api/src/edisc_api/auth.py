"""Authentication and tenant resolution (ADR 0013 section 2).

The tenant is NEVER taken from the request body or query: it is resolved from the ``Host`` subdomain,
the bearer token must be issued by one of THAT tenant's identity providers, and the token's subject
must be an active principal of that tenant. Unknown subdomains and every authentication failure get the
same 401, so subdomains cannot be enumerated.

Token checks: signature against the IdP's JWKS (cached, refreshed on an unknown ``kid``, refresh
rate-limited), algorithm allowlist (``RS256``, ``ES256``; never ``none`` or HMAC), ``iss``, ``aud``,
``exp`` / ``nbf`` with bounded leeway, ``sub`` present.

Local/test/ci only: the dev IdP (``EDISC_API_DEV_IDP``) signs tokens with a key in the local KMS
directory; tenant IdPs registered with ``jwks_url = "dev:"`` use it. Refused elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_core.logs import get_logger
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx

log = get_logger(__name__)

ALLOWED_ALGS = ("RS256", "ES256")
DEV_JWKS = "dev:"
DEV_ISSUER = "https://dev-idp.edisc.localhost"
DEV_KID = "edisc-dev-1"


class AuthError(Exception):
    """Any authentication failure. Always rendered as the same 401 (no enumeration)."""


@dataclass(frozen=True)
class Caller:
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    kind: str  # user | service
    issuer: str
    subject: str
    group_ids: frozenset[uuid.UUID]

    @property
    def actor(self) -> str:
        """How custody and audit events name this caller."""
        return f"{self.kind}:{self.principal_id}"


def host_subdomain(host: str | None, base_domain: str) -> str | None:
    if not host:
        return None
    name = host.split(":", 1)[0].lower().rstrip(".")
    suffix = "." + base_domain.lower()
    if not name.endswith(suffix):
        return None
    sub = name[: -len(suffix)]
    return sub if sub and "." not in sub else None


# ------------------------------------------------------------------ dev IdP (local/test/ci only)
def _dev_key_path(settings: Settings) -> Path:
    return settings.local_kms_dir / "dev-idp-key.pem"


def dev_private_key(settings: Settings) -> rsa.RSAPrivateKey:
    if not settings.api_dev_idp:
        raise AuthError("dev IdP disabled")
    path = _dev_key_path(settings)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pem)
        tmp.chmod(0o600)
        tmp.replace(path)
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise AuthError("dev IdP key is not RSA")
    return loaded


def dev_token(
    settings: Settings,
    *,
    subject: str,
    audience: str,
    groups: list[str] | None = None,
    lifetime: int = 3600,
    issuer: str = DEV_ISSUER,
    **claims: Any,
) -> str:
    """Issue a dev token (tests, local tools). Raises unless the dev IdP is enabled."""
    now = int(time.time())
    payload = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
        **({"groups": groups} if groups is not None else {}),
        **claims,
    }
    return jwt.encode(
        payload, dev_private_key(settings), algorithm="RS256", headers={"kid": DEV_KID}
    )


# ------------------------------------------------------------------ JWKS cache
@dataclass
class _Jwks:
    keys: dict[str, Any]
    fetched_at: float


@dataclass
class JwksCache:
    settings: Settings
    http: httpx.AsyncClient
    _cache: dict[str, _Jwks] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def key(self, jwks_url: str, kid: str | None) -> Any:
        if jwks_url == DEV_JWKS:
            if not self.settings.api_dev_idp:
                raise AuthError("dev IdP disabled")
            return dev_private_key(self.settings).public_key()
        entry = self._cache.get(jwks_url)
        fresh = (
            entry is not None
            and time.monotonic() - entry.fetched_at < self.settings.api_jwks_cache_seconds
        )
        if (
            entry is None
            or not fresh
            or (kid not in entry.keys and time.monotonic() - entry.fetched_at > 30)
        ):
            entry = await self._refresh(jwks_url)
        if kid is None and len(entry.keys) == 1:
            return next(iter(entry.keys.values()))
        if kid not in entry.keys:
            raise AuthError("unknown signing key")
        return entry.keys[kid]

    async def _refresh(self, jwks_url: str) -> _Jwks:
        lock = self._locks.setdefault(jwks_url, asyncio.Lock())
        async with lock:
            response = await self.http.get(jwks_url, timeout=5)
            response.raise_for_status()
            keys: dict[str, Any] = {}
            for jwk in response.json().get("keys", []):
                if jwk.get("use", "sig") != "sig":
                    continue
                keys[str(jwk.get("kid"))] = jwt.PyJWK(jwk).key
            entry = _Jwks(keys, time.monotonic())
            self._cache[jwks_url] = entry
            return entry


# ------------------------------------------------------------------ authenticate
@dataclass
class Authenticator:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    jwks: JwksCache

    async def tenant_for_host(self, host: str | None) -> uuid.UUID:
        sub = host_subdomain(host, self.settings.api_base_domain)
        if sub is None:
            raise AuthError("no tenant subdomain")
        async with self.sessions() as s, s.begin():
            tenant: uuid.UUID | None = (
                await s.execute(text("SELECT tenant_id_for_subdomain(:s)"), {"s": sub})
            ).scalar_one()
        if tenant is None:
            raise AuthError("unknown tenant")
        return tenant

    async def authenticate(self, host: str | None, authorization: str | None) -> Caller:
        tenant_id = await self.tenant_for_host(host)
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("missing bearer token")
        token = authorization[7:].strip()
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise AuthError("malformed token") from exc
        if header.get("alg") not in ALLOWED_ALGS:
            raise AuthError("algorithm not allowed")
        issuer = unverified.get("iss")
        if not isinstance(issuer, str):
            raise AuthError("no issuer")
        async with tenant_tx(self.sessions, tenant_id) as s:
            idp = (
                await s.execute(
                    text(
                        "SELECT audience, jwks_url, groups_claim FROM tenant_idps WHERE issuer = :i"
                    ),
                    {"i": issuer},
                )
            ).one_or_none()
        if idp is None:
            raise AuthError("issuer not configured for this tenant")
        key = await self.jwks.key(idp.jwks_url, header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(ALLOWED_ALGS),
                audience=idp.audience,
                issuer=issuer,
                leeway=self.settings.api_jwt_leeway_seconds,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {type(exc).__name__}") from exc
        subject = str(claims["sub"])
        external_groups = claims.get(idp.groups_claim) or []
        if not isinstance(external_groups, list):
            external_groups = []
        async with tenant_tx(self.sessions, tenant_id) as s:
            principal = (
                await s.execute(
                    text(
                        "SELECT id, kind, active FROM principals WHERE issuer = :i AND subject = :s"
                    ),
                    {"i": issuer, "s": subject},
                )
            ).one_or_none()
            if principal is None or not principal.active:
                raise AuthError("unknown or inactive principal")
            result = await s.execute(
                text(
                    "SELECT id FROM groups WHERE external_id = ANY(:ext)"
                    " UNION SELECT group_id FROM group_members WHERE principal_id = :p AND removed_at IS NULL"
                ),
                {"ext": [str(g) for g in external_groups], "p": principal.id},
            )
            group_ids: frozenset[uuid.UUID] = frozenset(result.scalars())
        return Caller(tenant_id, principal.id, principal.kind, issuer, subject, group_ids)


def jwks_document(settings: Settings) -> dict[str, Any]:
    """The dev IdP's public JWKS (served by tests that need an HTTP JWKS endpoint)."""
    public = dev_private_key(settings).public_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public))
    return {"keys": [{**jwk, "kid": DEV_KID, "use": "sig", "alg": "RS256"}]}
