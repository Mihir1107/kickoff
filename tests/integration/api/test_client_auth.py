"""`private_key_jwt` client authentication at the sign-in IdP's token endpoint (M17 step 3b, plan
§1.6, ADR 0016 step-3b notes).

The test IdP (`oidc.py`) treats a client registered with our PUBLISHED JWKS as confidential and
checks every assertion the way Entra ID and Okta document it; each refused variant here proves one
of its checks is live (so the successful sign-ins prove our assertions pass them). Expected values
come from the published documents and the IdP's own records, never from the signer under test."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from edisc_api import client_auth
from edisc_api.client_auth import ASSERTION_TYPE, ClientSigner
from edisc_core.kms import LocalKmsClient
from edisc_core.settings import Environment
from edisc_db.session import tenant_tx
from tests.logcapture import captured_logs

from .conftest import SECRETS, Api, TenantCtx
from .oidc import LOGIN_CLIENT, TEST_IDP, TOKEN_ENDPOINT, Browser
from .sessions_flow import enable_login, finish_login, host_of, sign_in, start_login

JWKS_PATH = "/.well-known/edisc-client-jwks.json"
CERT_PATH = "/.well-known/edisc-client-cert.pem"
PRIVATE_MEMBERS = {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}


@pytest.fixture
async def t(api: Api, tenant: TenantCtx) -> TenantCtx:
    await enable_login(api, tenant)
    return tenant


@pytest.fixture
def assertions(api: Api, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every assertion the API builds, registered as a credential canary: the response scan of the
    API fixture fails any test whose responses carry one."""
    made: list[str] = []
    signer = api.resources.signer
    real = signer.assertion

    async def recording(**kw: Any) -> str:
        value = await real(**kw)
        made.append(value)
        SECRETS.add(value)
        return value

    monkeypatch.setattr(signer, "assertion", recording)
    return made


async def _get(api: Api, t: TenantCtx, path: str, host: str | None = None) -> httpx.Response:
    async with api.client(t.subdomain) as c:
        return await c.get(f"http://{host or api.settings.api_base_domain}{path}")


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


async def _sessions(api: Api, t: TenantCtx) -> int:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return int((await s.execute(text("SELECT count(*) FROM sessions"))).scalar_one())


def _token_form() -> dict[str, str]:
    (form,) = [f for f in TEST_IDP.token_forms if f.get("grant_type") == "authorization_code"][-1:]
    return form


# ------------------------------------------------------------------ the successful exchange
async def test_sign_in_authenticates_with_a_client_assertion(
    api: Api, t: TenantCtx, assertions: list[str]
) -> None:
    browser = Browser()
    for _ in range(2):  # two sign-ins: each assertion is fresh (the IdP refuses a replayed jti)
        r = await sign_in(api, t, browser)
        assert r.status_code == 200, r.text
    assert TEST_IDP.refusals == [] and len(assertions) == 2 and await _sessions(api, t) == 2
    form = _token_form()
    assert form["client_assertion_type"] == ASSERTION_TYPE
    assert form["client_assertion"] == assertions[-1] and "client_secret" not in form
    assert form["code_verifier"]  # PKCE kept: both, never one instead of the other
    # checked against what the base host PUBLISHES (what an IdP administrator registers)
    published = (await _get(api, t, JWKS_PATH)).json()
    header = jwt.get_unverified_header(assertions[-1])
    (key,) = [k for k in published["keys"] if k["kid"] == header["kid"]]
    claims = jwt.decode(
        assertions[-1], jwt.PyJWK(key).key, algorithms=["RS256"], audience=TOKEN_ENDPOINT
    )
    assert claims["iss"] == claims["sub"] == LOGIN_CLIENT
    assert claims["exp"] - claims["iat"] == 300 and claims["nbf"] == claims["iat"]
    assert abs(claims["iat"] - time.time()) < 30
    assert len({jwt.decode(a, options={"verify_signature": False})["jti"] for a in assertions}) == 2
    assert header["x5t#S256"] == key["x5t#S256"] and header["x5t"] == key["x5t"]


async def test_a_public_client_sends_no_assertion_in_local_test_ci(
    api: Api, tenant: TenantCtx, assertions: list[str]
) -> None:
    await enable_login(api, tenant, client_auth="none")
    r = await sign_in(api, tenant, Browser())
    assert r.status_code == 200, r.text
    form = _token_form()
    assert "client_assertion" not in form and "client_assertion_type" not in form
    assert assertions == []


# ------------------------------------------------------------------ refused assertions
def _bad_form(**change: Any) -> Callable[..., Any]:
    """A ``ClientSigner.form`` replacement producing one kind of wrong client authentication."""

    async def form(self: ClientSigner, *, client_id: str, audience: str) -> Mapping[str, str]:
        if change.get("secret"):
            return {"client_secret": "not-a-secret-we-have"}
        if change.get("missing"):
            return {}
        kw: dict[str, Any] = {"client_id": client_id, "audience": change.get("aud", audience)}
        if "age" in change:
            kw["now"] = time.time() - change["age"]
        if "jti" in change:
            kw["jti"] = change["jti"]
        return {"client_assertion_type": ASSERTION_TYPE,
                "client_assertion": await self.assertion(**kw)}  # fmt: skip

    return form


REFUSED = {
    "wrong_audience": ({"aud": "https://dev-idp.edisc.localhost/other"}, "audience"),
    "expired": ({"age": 3600}, "expired"),
    "missing_assertion": ({"missing": True}, "no_assertion"),
    "client_secret_instead": ({"secret": True}, "client_secret"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
async def test_a_refused_client_assertion_fails_the_sign_in(
    api: Api, t: TenantCtx, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    change, why = REFUSED[case]
    signer = api.resources.signer
    monkeypatch.setattr(signer, "form", _bad_form(**change).__get__(signer))
    await _refused(api, t, why)


async def test_a_replayed_jti_is_refused(
    api: Api, t: TenantCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = api.resources.signer
    monkeypatch.setattr(signer, "form", _bad_form(jti="fixed-jti-for-the-replay").__get__(signer))
    assert (await sign_in(api, t, Browser())).status_code == 200  # first use of the jti
    await _refused(api, t, "replayed_jti", sessions_before=1)


async def test_an_assertion_signed_by_another_key_is_refused(api: Api, t: TenantCtx) -> None:
    # the IdP holds another public key under our kid (a stale upload, a key we do not hold)
    other = await asyncio.to_thread(rsa.generate_private_key, 65537, 3072)
    numbers = other.public_key().public_numbers()
    (ours,) = TEST_IDP.clients[LOGIN_CLIENT]["keys"]
    swapped = {
        **ours,
        "n": client_auth._int_b64url(numbers.n),
        "e": client_auth._int_b64url(numbers.e),
    }
    TEST_IDP.register_client(LOGIN_CLIENT, {"keys": [swapped]})
    await _refused(api, t, "signature")


async def _refused(api: Api, t: TenantCtx, why: str, *, sessions_before: int = 0) -> None:
    TEST_IDP.refusals.clear()
    r = await sign_in(api, t, Browser())
    assert r.status_code == 400, r.text
    assert "url=/?auth_error=invalid" in r.text
    assert TEST_IDP.refusals == [why]
    failed = (await _audit(api, t, "login_failed"))[-1]
    assert failed["reason"] == "invalid" and failed["detail"] == "exchange_invalid_client"
    assert await _sessions(api, t) == sessions_before  # no session from a refused exchange


# ------------------------------------------------------------------ the public client in production
def _staged(api: Api) -> Api:
    """The same API in a staging environment (only the client-authentication rule differs: the
    keys and connectors are handed over, as production builds them from its own settings)."""
    settings = api.settings.model_copy(update={"env": Environment.STAGING})
    resources = dataclasses.replace(
        api.resources, settings=settings, connectors={}, keyring=api.resources.keys,
        client_signer=api.resources.signer,
    )  # fmt: skip
    return Api(settings, api.sessions, api.s3, api.temporal, resources, api.http)


async def test_a_public_client_is_refused_outside_local_test_ci(
    api: Api, tenant: TenantCtx
) -> None:
    await enable_login(api, tenant, client_auth="none")
    staged = _staged(api)
    r = await start_login(staged, tenant, Browser())
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}  # the generic 401
    assert "location" not in r.headers and TEST_IDP.grants == {}
    (failed,) = await _audit(api, tenant, "login_failed")
    assert failed["reason"] == "client_auth_not_permitted" and failed["actor"] == "anonymous"
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        kinds = (await s.execute(text("SELECT kind FROM alerts"))).scalars().all()
        attempts = (await s.execute(text("SELECT count(*) FROM login_attempts"))).scalar_one()
    assert kinds == ["client_auth_not_permitted"] and attempts == 0
    # the same tenant as a confidential client signs in in staging
    await enable_login(api, tenant)
    assert (await sign_in(staged, tenant, Browser())).status_code == 200


async def test_a_switch_to_a_public_client_during_the_sign_in_is_refused_at_the_callback(
    api: Api, t: TenantCtx
) -> None:
    staged, browser = _staged(api), Browser()
    started = await start_login(staged, t, browser)
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(text("UPDATE tenant_idps SET login_client_auth = 'none'"))
    r = await finish_login(staged, t, browser, started, subject=t.admin_subject)
    assert r.status_code == 400 and "url=/?auth_error=invalid" in r.text
    assert (await _audit(api, t, "login_failed"))[-1]["detail"] == "client_auth_not_permitted"
    assert TEST_IDP.token_forms == [] and await _sessions(api, t) == 0


# ------------------------------------------------------------------ the IdP's advertised support
@pytest.mark.parametrize(
    ("extra", "detail"),
    [
        (
            {"token_endpoint_auth_methods_supported": ["client_secret_basic"]},
            "idp_no_private_key_jwt",
        ),
        ({"token_endpoint_auth_signing_alg_values_supported": ["PS256"]}, "idp_no_rs256"),
    ],
)
async def test_an_idp_that_cannot_take_our_assertion_fails_closed_at_the_start(
    api: Api, t: TenantCtx, extra: dict[str, Any], detail: str
) -> None:
    TEST_IDP.discovery_extra = extra
    r = await start_login(api, t, Browser())
    assert r.status_code == 400 and "url=/?auth_error=invalid" in r.text  # never sent to the IdP
    assert (await _audit(api, t, "login_failed"))[-1]["detail"] == detail


async def test_an_idp_that_does_not_publish_its_methods_is_used(api: Api, t: TenantCtx) -> None:
    TEST_IDP.discovery_extra = {
        "token_endpoint_auth_methods_supported": None,
        "token_endpoint_auth_signing_alg_values_supported": None,
    }
    assert (await sign_in(api, t, Browser())).status_code == 200


# ------------------------------------------------------------------ what is published
async def test_the_published_jwks_and_certificate_hold_only_the_signing_public_key(
    api: Api, t: TenantCtx, assertions: list[str], kms: LocalKmsClient
) -> None:
    jwks_r, cert_r = await _get(api, t, JWKS_PATH), await _get(api, t, CERT_PATH)
    assert jwks_r.status_code == cert_r.status_code == 200
    assert (
        jwks_r.headers["cache-control"] == cert_r.headers["cache-control"] == "public, max-age=300"
    )
    assert cert_r.headers["content-type"] == "application/x-pem-file"
    (key,) = jwks_r.json()["keys"]
    assert not PRIVATE_MEMBERS & set(key) and key["kty"] == "RSA" and key["alg"] == "RS256"
    assert key["use"] == "sig"
    pem = cert_r.content
    assert pem.count(b"-----BEGIN ") == pem.count(b"-----BEGIN CERTIFICATE-----") == 1
    cert = x509.load_pem_x509_certificate(pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    cert.verify_directly_issued_by(cert)  # self-signed by the KMS key
    public = cert.public_key()
    assert isinstance(public, rsa.RSAPublicKey) and public.key_size == 3072
    numbers = public.public_numbers()
    assert key["n"] == client_auth._int_b64url(numbers.n) and key["e"] == "AQAB"
    assert key["x5c"] == [base64.b64encode(der).decode()]
    assert key["x5t#S256"] == client_auth.b64url(hashlib.sha256(der).digest())
    thumbprint_input = json.dumps(
        {"e": key["e"], "kty": "RSA", "n": key["n"]}, separators=(",", ":")
    )
    assert key["kid"] == client_auth.b64url(hashlib.sha256(thumbprint_input.encode()).digest())
    # the private key the local KMS holds appears in neither document
    private = await asyncio.to_thread(kms._signing_key, client_auth.DEV_KEY.kms_key_id)
    d = client_auth._int_b64url(private.private_numbers().d)
    assert d not in jwks_r.text and d not in pem.decode()
    # the key that signs is the published one
    r = await sign_in(api, t, Browser())
    assert r.status_code == 200
    jwt.decode(assertions[-1], public, algorithms=["RS256"], audience=TOKEN_ENDPOINT)
    # every replica (a fresh signer over the same KMS key) publishes the same bytes
    fresh = ClientSigner.from_settings(api.settings, kms)
    assert await fresh.jwks() == jwks_r.json() and await fresh.certificates_pem() == pem


async def test_the_published_keys_are_served_on_the_base_host_only(api: Api, t: TenantCtx) -> None:
    for path in (JWKS_PATH, CERT_PATH):
        r = await _get(api, t, path, host=host_of(api, t))
        assert r.status_code == 404 and r.json()["error"] == "not_found"


async def test_no_assertion_or_key_handle_reaches_a_response_a_log_line_or_an_audit_payload(
    api: Api, t: TenantCtx, assertions: list[str], kms: LocalKmsClient
) -> None:
    handle = client_auth.DEV_KEY.kms_key_id
    SECRETS.add(handle)  # the response scan fails on the KMS key handle too
    private = await asyncio.to_thread(kms._signing_key, handle)
    pkcs8 = private.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    private_b64 = base64.b64encode(pkcs8).decode()[100:160]  # a slice of the key material
    with captured_logs() as logs:
        ok = await sign_in(api, t, Browser())
        TEST_IDP.register_client(LOGIN_CLIENT, {"keys": []})  # now the IdP refuses ours
        refused = await sign_in(api, t, Browser())
        logs.require("request", route="/v1/auth/callback", status=200)
        logs.require("request", route="/v1/auth/callback", status=400)
    assert ok.status_code == 200 and refused.status_code == 400 and len(assertions) == 2
    assert TEST_IDP.refusals == ["unknown_kid"]
    text_out = logs.getvalue()
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        stored = json.dumps(
            [
                str(r)
                for r in (
                    await s.execute(
                        text(
                            "SELECT payload FROM custody_events WHERE tenant_id = :t"
                            " UNION ALL SELECT to_jsonb(a) FROM login_attempts a"
                            " UNION ALL SELECT to_jsonb(x) FROM alerts x"
                        ),
                        {"t": t.tenant_id},
                    )
                ).all()
            ]
        )
    for value in (*assertions, *(a.rsplit(".", 1)[1] for a in assertions), handle, private_b64):
        assert value not in text_out and value not in stored
