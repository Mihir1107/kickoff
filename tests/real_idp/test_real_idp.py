"""`private_key_jwt` against a REAL Entra ID or Okta developer tenant (M17 step 3b).

Skipped, with this reason printed, when ``EDISC_REAL_IDP_ISSUER`` is not set. Never silently
passed: if ANY ``EDISC_REAL_IDP_*`` variable is set, every required one must be, or every test here
FAILS (a half-configured run is a mistake, not a skip).

Setup (once per tenant; the claims and settings each test confirms are listed in ADR 0016, "M17
step 3b", "To confirm on real tenants"):

1. ``uv run python scripts/real_idp.py prepare --dir ~/edisc-real-idp`` (a key in a local KMS
   directory; writes ``client-cert.pem`` and ``client-jwks.json``).
2. Register a WEB application (confidential client) with the authorization-code grant and redirect
   URI ``EDISC_REAL_IDP_REDIRECT_URI`` (default ``http://localhost:8765/callback``):
   - Entra ID: upload ``client-cert.pem`` under Certificates & secrets; no client secret.
   - Okta: client authentication "Public key / Private key", add the key from
     ``client-jwks.json``; "Require PKCE" on.
3. Run::

       EDISC_REAL_IDP=entra|okta \
       EDISC_REAL_IDP_ISSUER=https://login.microsoftonline.com/<tid>/v2.0 \
       EDISC_REAL_IDP_CLIENT_ID=<application (client) id> \
       EDISC_REAL_IDP_KEY_DIR=~/edisc-real-idp \
       [EDISC_REAL_IDP_SCOPE=<client id>/.default]      # also run the client-credentials leg
       [EDISC_REAL_IDP_INTERACTIVE=1]                    # also sign in a person in the browser
       make test-real-idp

What the tests prove without a person signing in: the discovery document advertises
``private_key_jwt`` + RS256; the token endpoint does NOT answer ``invalid_client`` to our
assertion (an unknown code gives ``invalid_grant``) but DOES answer ``invalid_client`` to the same
request signed by a key it does not hold, to a wrong ``aud`` and to a missing assertion (so the
first result means our assertion was verified, not ignored); with a scope, a client-credentials
token is ISSUED on our assertion alone. Interactive: the full code exchange with PKCE and our
assertion, and the ID token's claims checked as the product checks them (printed for the ADR).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from scripts.real_idp import load

from edisc_api.auth import JwksCache
from edisc_api.client_auth import ASSERTION_TYPE, ClientSigner, b64url
from edisc_api.oidc import OidcClient, Provider, check_client_auth
from edisc_core.settings import Environment, Settings

REQUIRED = ("EDISC_REAL_IDP", "EDISC_REAL_IDP_ISSUER", "EDISC_REAL_IDP_CLIENT_ID",
            "EDISC_REAL_IDP_KEY_DIR")  # fmt: skip
PROVIDERS = ("entra", "okta")


def _config() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.startswith("EDISC_REAL_IDP")}
    if not env:
        pytest.skip(
            "real IdP tests need a developer tenant: set "
            + ", ".join(REQUIRED)
            + " (tests/real_idp/test_real_idp.py docstring)"
        )
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        pytest.fail(f"half-configured real IdP run: missing {', '.join(missing)}")
    if env["EDISC_REAL_IDP"] not in PROVIDERS:
        pytest.fail(f"EDISC_REAL_IDP must be one of {PROVIDERS}")
    return env


@dataclass
class Real:
    provider_name: str
    client_id: str
    provider: Provider
    signer: ClientSigner
    http: httpx.AsyncClient
    oidc: OidcClient
    env: dict[str, str]


@pytest.fixture
async def real() -> AsyncIterator[Real]:
    env = _config()
    kms, spec = await asyncio.to_thread(load, Path(env["EDISC_REAL_IDP_KEY_DIR"]))
    settings = Settings(env=Environment.LOCAL)
    async with httpx.AsyncClient() as http:
        oidc = OidcClient(settings, http, JwksCache(settings, http))
        provider = await oidc.provider(env["EDISC_REAL_IDP_ISSUER"])
        yield Real(env["EDISC_REAL_IDP"], env["EDISC_REAL_IDP_CLIENT_ID"], provider,
                   ClientSigner(kms, (spec,)), http, oidc, env)  # fmt: skip


def _report(what: str, value: object) -> None:
    print(f"[real-idp] {what}: {json.dumps(value, default=str)}")


async def _token(real: Real, client_auth: dict[str, str], **form: str) -> httpx.Response:
    return await real.http.post(
        real.provider.token_endpoint,
        data={"client_id": real.client_id, **form, **client_auth},
        headers={"Accept": "application/json"},
        timeout=15,
    )


def _code_form(real: Real) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "code": "edisc-probe-" + secrets.token_urlsafe(16),  # no such code
        "redirect_uri": real.env.get(
            "EDISC_REAL_IDP_REDIRECT_URI", "http://localhost:8765/callback"
        ),
        "code_verifier": secrets.token_urlsafe(48),
    }


def _error(r: httpx.Response) -> str:
    body = r.json() if r.content else {}
    _report("token endpoint answer", {"status": r.status_code, "error": body.get("error"),
                                      "codes": body.get("error_codes")})  # fmt: skip
    return str(body.get("error"))


async def _forged(real: Real, *, audience: str | None = None, key: Any = None) -> str:
    """Our real header (kid, x5t) and claims, signed by ``key`` (default: OUR key)."""
    if key is None:
        return await real.signer.assertion(
            client_id=real.client_id, audience=audience or real.provider.token_endpoint
        )
    (published,) = await real.signer.keys()
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": published.kid, "x5t": published.x5t,
              "x5t#S256": published.x5t_s256}  # fmt: skip
    claims = {"iss": real.client_id, "sub": real.client_id,
              "aud": audience or real.provider.token_endpoint,
              "jti": secrets.token_urlsafe(32), "iat": now, "nbf": now, "exp": now + 300}  # fmt: skip
    return jwt.encode(claims, key, algorithm="RS256", headers=header)


def _assert(value: str) -> dict[str, str]:
    return {"client_assertion_type": ASSERTION_TYPE, "client_assertion": value}


async def test_discovery_advertises_private_key_jwt_and_rs256(real: Real) -> None:
    _report("discovery", {
        "token_endpoint": real.provider.token_endpoint,
        "token_endpoint_auth_methods_supported": real.provider.auth_methods,
        "token_endpoint_auth_signing_alg_values_supported": real.provider.auth_signing_algs,
    })  # fmt: skip
    check_client_auth(real.provider, "private_key_jwt")  # what the login start checks


async def test_the_token_endpoint_verifies_our_assertion(real: Real) -> None:
    """Ours: anything but invalid_client (an unknown code). A key it does not hold, a wrong aud,
    no assertion: invalid_client. Both halves together show the assertion is VERIFIED."""
    form = _code_form(real)
    ours = _error(await _token(real, _assert(await _forged(real)), **form))
    assert ours != "invalid_client", "the IdP refused our client assertion"
    other_key = await asyncio.to_thread(rsa.generate_private_key, 65537, 3072)
    refused = {
        "other_key": _assert(await _forged(real, key=other_key)),
        "wrong_aud": _assert(await _forged(real, audience=real.provider.issuer)),
        "missing": {},
    }
    for case, client_auth in refused.items():
        got = _error(await _token(real, client_auth, **_code_form(real)))
        assert got == "invalid_client", (
            f"{case}: answered {got!r}, not invalid_client: this IdP does not check client "
            "authentication before the code, so the first result proves nothing; use "
            "EDISC_REAL_IDP_SCOPE (client credentials) or EDISC_REAL_IDP_INTERACTIVE"
        )


async def test_a_replayed_assertion_is_refused(real: Real) -> None:
    """Okta documents jti replay rejection; whether Entra does is one of the items to confirm."""
    value = _assert(await _forged(real))
    first = _error(await _token(real, value, **_code_form(real)))
    second = _error(await _token(real, value, **_code_form(real)))
    _report("replay", {"first": first, "second": second})
    assert first != "invalid_client" and second == "invalid_client"


async def test_client_credentials_are_issued_on_our_assertion(real: Real) -> None:
    scope = real.env.get("EDISC_REAL_IDP_SCOPE")
    if not scope:
        pytest.skip("EDISC_REAL_IDP_SCOPE not set: the client-credentials leg is not run")
    r = await _token(
        real, _assert(await _forged(real)), grant_type="client_credentials", scope=scope
    )
    assert r.status_code == 200, _error(r)
    body = r.json()
    assert body.get("token_type", "").lower() == "bearer" and body.get("access_token")
    _report(
        "client credentials", {"expires_in": body.get("expires_in"), "scope": body.get("scope")}
    )


@pytest.mark.timeout(330)
async def test_interactive_sign_in_with_pkce_and_our_assertion(real: Real) -> None:
    if real.env.get("EDISC_REAL_IDP_INTERACTIVE") != "1":
        pytest.skip("EDISC_REAL_IDP_INTERACTIVE not 1: no person signs in")
    redirect = real.env.get("EDISC_REAL_IDP_REDIRECT_URI", "http://localhost:8765/callback")
    parts = urllib.parse.urlsplit(redirect)
    state, nonce, verifier = (
        secrets.token_urlsafe(24),
        secrets.token_urlsafe(24),
        secrets.token_urlsafe(48),
    )
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    got: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode()
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(line.split(" ")[1]).query))
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\nedisc: you may close this tab\n"
        )
        await writer.drain()
        writer.close()
        if not got.done() and ("code" in query or "error" in query):
            got.set_result(query)

    server = await asyncio.start_server(handle, parts.hostname, parts.port)
    async with server:
        url = real.oidc.authorize_url(
            real.provider, client_id=real.client_id, redirect_uri=redirect, state=state,
            nonce=nonce, challenge=challenge, reauth=False,
        )  # fmt: skip
        print(f"[real-idp] sign in here if no browser opened: {url}")
        webbrowser.open(url)
        query = await asyncio.wait_for(got, 300)
    assert query.get("state") == state and "code" in query, f"IdP answered {query.get('error')}"
    form = await real.signer.form(client_id=real.client_id, audience=real.provider.token_endpoint)
    id_token = await real.oidc.exchange(
        real.provider, client_id=real.client_id, redirect_uri=redirect, code=query["code"],
        verifier=verifier, client_auth=dict(form),
    )  # fmt: skip
    header = jwt.get_unverified_header(id_token)
    unverified = jwt.decode(id_token, options={"verify_signature": False})
    jwks_url = real.env.get("EDISC_REAL_IDP_JWKS_URL") or await _discovered_jwks(real)
    claims = await real.oidc.verify(
        id_token, issuer=real.provider.issuer, jwks_url=jwks_url, client_id=real.client_id,
        nonce=nonce, groups_claim="groups",
    )  # fmt: skip
    _report("id_token", {
        "alg": header.get("alg"), "claims": sorted(unverified), "iss": unverified.get("iss"),
        "aud": unverified.get("aud"), "auth_time": "auth_time" in unverified,
        "groups": "groups" in unverified, "overage": "_claim_names" in unverified,
        "sub_vs_oid": {"sub": bool(unverified.get("sub")), "oid": "oid" in unverified},
    })  # fmt: skip
    assert claims.subject == unverified["sub"]


async def _discovered_jwks(real: Real) -> str:
    """The product reads keys only from the REGISTERED ``jwks_url``; this run takes the discovery
    document's ``jwks_uri`` (printed, so the registered value can be set from it)."""
    r = await real.http.get(real.provider.issuer.rstrip("/") + "/.well-known/openid-configuration")
    uri = str(r.json()["jwks_uri"])
    _report("jwks_uri", uri)
    return uri
