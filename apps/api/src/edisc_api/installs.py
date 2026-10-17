"""Source install flows: signed state, the providers' server-side legs (M17 step 6; ADR 0016 §5,
A2, A3, A4).

- **State** (A3): ``v1.<kid>.<install id>.<expiry>.<mac>``; the MAC (HMAC-SHA-256 under a key
  derived from the API keyring) covers the install id, the expiry, OUR tenant id, the principal id
  and the install's nonce. Tenant, user and nonce are bound by the MAC, never carried in clear.
  Single use (consumed atomically at the tenant host), 10 minutes.
- **PKCE** S256: the verifier is derived from the install cookie's handle under the keyring (like
  sign-in's), so no secret is stored. Microsoft always; Slack when ``slack_pkce`` is on (to confirm
  against our real Slack app, ADR 0016 step-6 notes).
- **Slack** (A4): the team id and enterprise id come from the authenticated ``oauth.v2.access``
  response (server-side, our client secret, TLS), never from the redirect. Its tokens are
  registered with the redactor the moment the body is parsed, before any branch.
- **Microsoft** (A4): consent through the OIDC authorization-code endpoint (``prompt=consent``,
  our app's ``.default`` scope, ``openid``, PKCE, nonce); the Entra tenant bound is the ``tid`` of
  the SIGNED ID token (Microsoft's keys, issuer ``https://login.microsoftonline.com/<tid>/v2.0``,
  audience, nonce). The redirect's ``tenant`` and ``admin_consent`` are never read. **The gate is
  Microsoft's admin consent, shown by a client-credentials token for that ``tid``** (our client
  assertion, over TLS from Microsoft's token endpoint) issued to OUR app in THAT tenant and
  carrying application roles: only a tenant administrator's consent grants application
  permissions, so a token without them means no consent and the install is refused. ``wids`` in
  the ID token is NOT required (Entra emits it only when the app registration asks for it): when
  present it must name an administrator role (a contradiction refuses), when absent the install
  records that the consent token alone proved it (``admin_evidence``); either way the consent
  token is always required. No Graph token is stored.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from pydantic import SecretStr

from edisc_api.auth import JwksCache
from edisc_api.client_auth import ClientSigner
from edisc_api.sessions import Keyring, pkce_challenge
from edisc_connector_slack import scopes as slack_scopes
from edisc_connector_teams import permissions as teams_permissions
from edisc_connectors_base.ratelimit import BucketKey, RateLimiter, call_with_limits
from edisc_core.redaction import register_secret
from edisc_core.settings import Settings

INSTALL_COOKIE = "__Host-edisc_install"
INSTALL_SECONDS = 600
PROVIDERS = ("slack", "microsoft")
SLACK_AUTHORIZE = "https://slack.com/oauth/v2/authorize"
SLACK_ACCESS = "https://slack.com/api/oauth.v2.access"
MS_LOGIN = "https://login.microsoftonline.com"
MS_AUTHORIZE = f"{MS_LOGIN}/organizations/oauth2/v2.0/authorize"
MS_TOKEN = f"{MS_LOGIN}/organizations/oauth2/v2.0/token"
MS_JWKS = f"{MS_LOGIN}/common/discovery/v2.0/keys"
GRAPH_DEFAULT = "https://graph.microsoft.com/.default"
# Entra role template ids that may grant tenant-wide admin consent to application permissions
ADMIN_ROLES = frozenset({
    "62e90394-69f5-4237-9190-012177145e10",  # Global Administrator
    "e8611ab8-c189-46e8-94e1-60213ab1f814",  # Privileged Role Administrator
})  # fmt: skip
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SLACK_ID = re.compile(r"^[A-Z0-9]{2,32}$")
_STATE = re.compile(
    r"^v1\.([A-Za-z0-9_-]{1,32})\.([0-9a-f-]{36})\.([0-9]{1,12})\.([A-Za-z0-9_-]{43})$"
)


class InstallError(Exception):
    """An install refused or failed: ``code`` from the fixed list the UI receives
    (``install_error``), ``detail`` a fixed detail code for the audit (never a provider's text)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


# ------------------------------------------------------------------ state (A3)
@dataclass(frozen=True)
class State:
    kid: str
    install_id: uuid.UUID
    expiry: int  # unix seconds
    mac: str

    def text(self) -> str:
        return f"v1.{self.kid}.{self.install_id}.{self.expiry}.{self.mac}"


def _message(install_id: uuid.UUID, expiry: int, tenant_id: uuid.UUID, principal_id: uuid.UUID,
             nonce: str) -> str:  # fmt: skip
    return f"edisc-install/v1|{install_id}|{expiry}|{tenant_id}|{principal_id}|{nonce}"


def sign_state(keys: Keyring, *, install_id: uuid.UUID, expires_at: datetime,
               tenant_id: uuid.UUID, principal_id: uuid.UUID, nonce: str) -> State:  # fmt: skip
    expiry = int(expires_at.timestamp())
    mac = keys.install_mac(
        keys.current, _message(install_id, expiry, tenant_id, principal_id, nonce)
    )
    return State(keys.current, install_id, expiry, mac)


def parse_state(keys: Keyring, value: str | None, *, now: float | None = None) -> State:
    """Format, a known key and an unexpired expiry: checked WITHOUT a database read."""
    found = _STATE.match(value or "") if value is not None and len(value) <= 160 else None
    if found is None:
        raise InstallError("invalid", "state_malformed")
    kid, install, expiry, mac = found.groups()
    try:
        install_id = uuid.UUID(install)
    except ValueError as exc:
        raise InstallError("invalid", "state_malformed") from exc
    if kid not in keys.keys:
        raise InstallError("invalid", "state_unknown_key")
    if int(expiry) <= (time.time() if now is None else now):
        raise InstallError("expired", "state_expired")
    return State(kid, install_id, int(expiry), mac)


def mac_matches(keys: Keyring, state: State, *, tenant_id: uuid.UUID, principal_id: uuid.UUID,
                nonce: str) -> bool:  # fmt: skip
    import hmac

    expected = keys.install_mac(
        state.kid, _message(state.install_id, state.expiry, tenant_id, principal_id, nonce)
    )
    return hmac.compare_digest(expected.encode(), state.mac.encode())


def relay_uri(settings: Settings, provider: str) -> str:
    """The ONE redirect URI registered with each provider (A3)."""
    host = f"{settings.api_oauth_relay_subdomain}.{settings.api_base_domain}"
    return f"{settings.api_public_scheme}://{host}/v1/oauth/{provider}/callback"


def relay_host(settings: Settings) -> str:
    return f"{settings.api_oauth_relay_subdomain}.{settings.api_base_domain}"


# ------------------------------------------------------------------ authorize URLs
def requested_scopes(provider: str) -> tuple[str, ...]:
    return slack_scopes.BOT_SCOPES if provider == "slack" else teams_permissions.APP_ROLES


def authorize_url(
    settings: Settings, provider: str, *, state: str, nonce: str, verifier: str
) -> str:
    if provider == "slack":
        if not settings.slack_client_id:
            raise InstallError("failed", "slack_not_configured")
        query = {"client_id": settings.slack_client_id, "scope": ",".join(slack_scopes.BOT_SCOPES),
                 "redirect_uri": relay_uri(settings, "slack"), "state": state}  # fmt: skip
        if settings.slack_pkce:
            query |= {"code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256"}
        return f"{SLACK_AUTHORIZE}?{urllib.parse.urlencode(query)}"
    if not settings.microsoft_client_id:
        raise InstallError("failed", "microsoft_not_configured")
    query = {
        "client_id": settings.microsoft_client_id, "response_type": "code",
        "redirect_uri": relay_uri(settings, "microsoft"), "response_mode": "query",
        "scope": f"openid {GRAPH_DEFAULT}", "prompt": "consent", "state": state, "nonce": nonce,
        "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256",
    }  # fmt: skip
    return f"{MS_AUTHORIZE}?{urllib.parse.urlencode(query)}"


# ------------------------------------------------------------------ the providers' answers
@dataclass(frozen=True)
class Grant:
    """What the provider's AUTHENTICATED answer says (never the redirect)."""

    external_id: str  # Slack team id / Entra tid
    enterprise_id: str | None  # Slack Enterprise Grid
    scopes: tuple[str, ...]  # granted (Slack scopes / Graph app roles)
    blind_spots: tuple[str, ...]
    access_token: SecretStr | None  # Slack only; Microsoft: none stored (client credentials later)
    refresh_token: SecretStr | None
    expires_at: datetime | None
    token_type: str | None
    # Microsoft: what showed tenant-wide admin consent: "consent_token" (the client-credentials
    # token's roles), "consent_token+wids" (and an administrator role in the ID token's wids)
    admin_evidence: str | None = None


async def slack_exchange(
    settings: Settings, http: httpx.AsyncClient, limiter: RateLimiter, *, tenant_id: uuid.UUID,
    code: str, verifier: str,
) -> Grant:  # fmt: skip
    if not settings.slack_client_id or settings.slack_client_secret is None:
        raise InstallError("failed", "slack_not_configured")
    form = {"client_id": settings.slack_client_id,
            "client_secret": settings.slack_client_secret.get_secret_value(), "code": code,
            "redirect_uri": relay_uri(settings, "slack")}  # fmt: skip
    if settings.slack_pkce:
        form["code_verifier"] = verifier

    async def post() -> httpx.Response:
        return await http.post(SLACK_ACCESS, data=form, timeout=10, follow_redirects=False)

    try:
        response = await call_with_limits(
            limiter, BucketKey(tenant_id, "slack", "-", "oauth.v2.access"), post
        )
        body = response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise InstallError("failed", "exchange_unreachable") from exc
    if isinstance(body, dict):  # the tokens are secrets from the moment they are parsed
        for name in ("access_token", "refresh_token"):
            if isinstance(body.get(name), str) and body[name]:
                register_secret(body[name])
    if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        raise InstallError("failed", "exchange_refused")
    team: dict[str, Any] = body["team"] if isinstance(body.get("team"), dict) else {}
    enterprise: dict[str, Any] = (
        body["enterprise"] if isinstance(body.get("enterprise"), dict) else {}
    )
    team_id, enterprise_id = team.get("id"), enterprise.get("id")
    if not isinstance(team_id, str) or not _SLACK_ID.match(team_id):
        raise InstallError("failed", "exchange_no_team")
    if enterprise_id is not None and (
        not isinstance(enterprise_id, str) or not _SLACK_ID.match(enterprise_id)
    ):
        raise InstallError("failed", "exchange_bad_enterprise")
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise InstallError("failed", "exchange_no_token")
    refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
    lifetime = body.get("expires_in")
    expires = (datetime.now(UTC) + timedelta(seconds=lifetime)
               if isinstance(lifetime, int) and lifetime > 0 else None)  # fmt: skip
    granted = slack_scopes.granted(body.get("scope"))
    token_type = body.get("token_type") if isinstance(body.get("token_type"), str) else None
    return Grant(team_id, enterprise_id, granted, slack_scopes.blind_spots(granted),
                 SecretStr(access), SecretStr(refresh) if refresh else None, expires,
                 token_type)  # fmt: skip


async def _ms_token(http: httpx.AsyncClient, limiter: RateLimiter, tenant_id: uuid.UUID,
                    url: str, form: dict[str, str]) -> dict[str, Any]:  # fmt: skip
    async def post() -> httpx.Response:
        return await http.post(url, data=form, timeout=10, follow_redirects=False,
                               headers={"Accept": "application/json"})  # fmt: skip

    try:
        response = await call_with_limits(
            limiter, BucketKey(tenant_id, "microsoft", "-", "token"), post
        )
        body = response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise InstallError("failed", "exchange_unreachable") from exc
    if isinstance(body, dict):
        for name in ("access_token", "refresh_token", "id_token"):
            if isinstance(body.get(name), str) and body[name]:
                register_secret(body[name])
    if response.status_code != 200 or not isinstance(body, dict):
        refused = isinstance(body, dict) and body.get("error") == "invalid_client"
        raise InstallError("failed", "exchange_invalid_client" if refused else "exchange_refused")
    return body


async def microsoft_exchange(
    settings: Settings, http: httpx.AsyncClient, limiter: RateLimiter, jwks: JwksCache,
    signer: ClientSigner, *, tenant_id: uuid.UUID, code: str, verifier: str, nonce: str,
) -> Grant:  # fmt: skip
    client_id = settings.microsoft_client_id
    if not client_id:
        raise InstallError("failed", "microsoft_not_configured")
    body = await _ms_token(http, limiter, tenant_id, MS_TOKEN, {
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": relay_uri(settings, "microsoft"), "code_verifier": verifier,
        "scope": f"openid {GRAPH_DEFAULT}",
        **await signer.form(client_id=client_id, audience=MS_TOKEN),
    })  # fmt: skip
    token = body.get("id_token")
    if not isinstance(token, str):
        raise InstallError("failed", "exchange_no_id_token")
    claims = await _verify_id_token(settings, jwks, token, client_id=client_id, nonce=nonce)
    tid = claims["tid"]
    evidence = "consent_token"
    if "wids" in claims:  # extra confirmation when Entra emits it; never required, never skipped
        wids = claims["wids"]
        if not isinstance(wids, list) or not all(isinstance(r, str) for r in wids):
            raise InstallError("refused", "wids_malformed")
        if not ADMIN_ROLES & set(wids):
            raise InstallError("refused", "not_tenant_admin")
        evidence = "consent_token+wids"
    # THE GATE: Microsoft's admin consent, shown by a client-credentials token for THAT tid
    granted = await _consented_roles(http, limiter, signer, tenant_id=tenant_id,
                                     client_id=client_id, tid=tid)  # fmt: skip
    return Grant(tid, None, granted, teams_permissions.blind_spots(granted), None, None, None,
                 "client_credentials", evidence)  # fmt: skip


async def _consented_roles(
    http: httpx.AsyncClient, limiter: RateLimiter, signer: ClientSigner, *, tenant_id: uuid.UUID,
    client_id: str, tid: str,
) -> tuple[str, ...]:  # fmt: skip
    """The application permissions a tenant administrator of ``tid`` granted OUR app. Microsoft
    issues a client-credentials token only to an app with a service principal in that tenant, and
    puts application roles in it only once an administrator consented: no token, a token for
    another tenant or app, or a token without any of our roles = no admin consent (refused)."""
    tenant_token = f"{MS_LOGIN}/{tid}/oauth2/v2.0/token"
    try:
        body = await _ms_token(http, limiter, tenant_id, tenant_token, {
            "grant_type": "client_credentials", "client_id": client_id, "scope": GRAPH_DEFAULT,
            **await signer.form(client_id=client_id, audience=tenant_token),
        })  # fmt: skip
    except InstallError as exc:
        if exc.detail == "exchange_unreachable":
            raise  # Microsoft did not answer: a failure to retry, not a refusal
        raise InstallError("refused", "consent_token_refused") from exc
    claims = _unverified_claims(body.get("access_token"))
    if claims.get("tid") != tid:
        raise InstallError("refused", "consent_token_tenant")
    if (claims.get("appid") or claims.get("azp")) != client_id:
        raise InstallError("refused", "consent_token_app")
    roles = claims.get("roles")
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise InstallError("refused", "admin_consent_missing")
    granted = teams_permissions.granted(roles)
    if not granted:
        raise InstallError("refused", "admin_consent_missing")
    return granted


async def _verify_id_token(settings: Settings, jwks: JwksCache, token: str, *, client_id: str,
                           nonce: str) -> dict[str, Any]:  # fmt: skip
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InstallError("failed", "id_token_malformed") from exc
    if header.get("alg") != "RS256":
        raise InstallError("failed", "id_token_algorithm")
    key = await jwks.key(MS_JWKS, header.get("kid"))
    try:
        claims: dict[str, Any] = jwt.decode(
            token, key, algorithms=["RS256"], audience=client_id,
            leeway=settings.api_jwt_leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud", "nonce", "tid"], "verify_iss": False},
        )  # fmt: skip
    except jwt.PyJWTError as exc:
        raise InstallError("failed", "id_token_rejected") from exc
    tid = claims.get("tid")
    if not isinstance(tid, str) or not _GUID.match(tid):
        raise InstallError("failed", "id_token_bad_tid")
    # multi-tenant: the issuer is the tid's own (an id token of tenant X never names tenant Y)
    if claims.get("iss") != f"{MS_LOGIN}/{tid}/v2.0":
        raise InstallError("failed", "id_token_issuer")
    if claims.get("nonce") != nonce:
        raise InstallError("failed", "id_token_nonce")
    return claims


def _unverified_claims(token: object) -> dict[str, Any]:
    """The claims of an access token we received over TLS from Microsoft's token endpoint (a Graph
    token is opaque to its client by contract: its signature is Graph's to check, not ours)."""
    if not isinstance(token, str) or token.count(".") != 2:
        raise InstallError("failed", "token_malformed")
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError as exc:
        raise InstallError("failed", "token_malformed") from exc
    return data if isinstance(data, dict) else {}
