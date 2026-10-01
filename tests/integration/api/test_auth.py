"""ADR 0013 section 2: the tenant comes from the Host AND the token's IdP AND an active principal."""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import text

from edisc_api.auth import DEV_ISSUER, dev_private_key, dev_token, jwks_document
from edisc_core.kms import LocalKmsClient
from edisc_db.session import tenant_tx

from .conftest import AUDIENCE, Api, TenantCtx, add_principal, new_api_tenant

UNAUTHORIZED = {"error": "unauthorized"}


async def _me(api: Api, subdomain: str, token: str | None, **kw: Any) -> httpx.Response:
    async with api.client(subdomain, token) as c:
        return await c.get("/v1/me", **kw)


async def test_valid_token_resolves_the_principal_of_the_hosts_tenant(
    api: Api, tenant: TenantCtx
) -> None:
    r = await _me(api, tenant.subdomain, tenant.token(api.settings))
    assert r.status_code == 200
    assert r.json()["principal_id"] == str(tenant.admin_id)


async def test_token_of_tenant_a_is_rejected_on_tenant_bs_host(
    api: Api, tenant: TenantCtx, kms: LocalKmsClient
) -> None:
    other = await new_api_tenant(
        api, kms
    )  # same dev issuer: only the principal lookup separates them
    r = await _me(api, other.subdomain, tenant.token(api.settings))
    assert (r.status_code, r.json()) == (401, UNAUTHORIZED)


async def test_unknown_subdomain_looks_exactly_like_a_bad_token(
    api: Api, tenant: TenantCtx
) -> None:
    unknown = await _me(api, "no-such-tenant", tenant.token(api.settings))
    bad = await _me(api, tenant.subdomain, "not-a-token")
    assert (unknown.status_code, unknown.json(), unknown.headers["www-authenticate"]) == (
        bad.status_code,
        bad.json(),
        bad.headers["www-authenticate"],
    )
    assert unknown.status_code == 401


async def test_forged_tenant_id_parameters_are_ignored(
    api: Api, tenant: TenantCtx, kms: LocalKmsClient
) -> None:
    other = await new_api_tenant(api, kms)
    r = await _me(
        api,
        tenant.subdomain,
        tenant.token(api.settings),
        params={"tenant_id": str(other.tenant_id)},
        headers={"x-tenant-id": str(other.tenant_id)},
    )
    assert r.status_code == 200 and r.json()["principal_id"] == str(tenant.admin_id)


@pytest.mark.parametrize(
    "case",
    [
        "wrong_audience",
        "expired",
        "not_yet_valid",
        "no_subject",
        "unknown_issuer",
        "alg_none",
        "hs256_confusion",
        "garbage_signature",
    ],
)
async def test_invalid_tokens_are_rejected(api: Api, tenant: TenantCtx, case: str) -> None:
    s = api.settings
    now = int(time.time())
    if case == "wrong_audience":
        token = dev_token(s, subject=tenant.admin_subject, audience="someone-else")
    elif case == "expired":
        token = dev_token(s, subject=tenant.admin_subject, audience=AUDIENCE, lifetime=-120)
    elif case == "not_yet_valid":
        token = dev_token(s, subject=tenant.admin_subject, audience=AUDIENCE, nbf=now + 600)
    elif case == "no_subject":
        key = dev_private_key(s)
        token = jwt.encode(
            {"iss": DEV_ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60},
            key,
            algorithm="RS256",
        )
    elif case == "unknown_issuer":
        token = dev_token(s, subject=tenant.admin_subject, audience=AUDIENCE, issuer="https://evil")
    elif case == "alg_none":
        token = jwt.encode(
            {
                "iss": DEV_ISSUER,
                "sub": tenant.admin_subject,
                "aud": AUDIENCE,
                "iat": now,
                "exp": now + 60,
            },
            None,
            algorithm="none",
        )
    elif case == "hs256_confusion":
        from cryptography.hazmat.primitives import serialization

        public_pem = (
            dev_private_key(s)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        header = jwt.utils.base64url_encode(b'{"alg":"HS256","typ":"JWT"}')
        body = jwt.utils.base64url_encode(
            jwt.utils.force_bytes(
                f'{{"iss":"{DEV_ISSUER}","sub":"{tenant.admin_subject}","aud":"{AUDIENCE}","iat":{now},"exp":{now + 60}}}'
            )
        )
        import hashlib
        import hmac

        sig = jwt.utils.base64url_encode(
            hmac.new(public_pem, header + b"." + body, hashlib.sha256).digest()
        )
        token = (header + b"." + body + b"." + sig).decode()
    else:  # garbage_signature
        good = tenant.token(s)
        token = good[: good.rindex(".") + 1] + "A" * 40
    r = await _me(api, tenant.subdomain, token)
    assert (r.status_code, r.json()) == (401, UNAUTHORIZED), case


async def test_deactivated_principal_is_rejected(api: Api, tenant: TenantCtx) -> None:
    _, subject = await add_principal(api, tenant)
    token = tenant.token(api.settings, subject=subject)
    assert (await _me(api, tenant.subdomain, token)).status_code == 200
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        await s.execute(
            text("UPDATE principals SET active = false WHERE subject = :s"), {"s": subject}
        )
    assert (await _me(api, tenant.subdomain, token)).status_code == 401


async def test_dev_issuer_is_refused_when_the_dev_idp_is_off(api: Api, tenant: TenantCtx) -> None:
    token = tenant.token(api.settings)
    api.resources.authenticator.settings = api.settings.model_copy(update={"api_dev_idp": False})
    api.resources.authenticator.jwks.settings = api.resources.authenticator.settings
    assert (await _me(api, tenant.subdomain, token)).status_code == 401


async def test_http_jwks_with_key_rotation(
    api: Api, tenant: TenantCtx, kms: LocalKmsClient
) -> None:
    """An IdP over HTTP (the only mocked piece: the external IdP). A new kid triggers one JWKS refresh."""
    calls: list[str] = []
    doc = jwks_document(api.settings)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=doc)

    api.resources.authenticator.jwks.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    t = await new_api_tenant(
        api, kms, issuer="https://idp.example/t", jwks_url="https://idp.example/t/jwks"
    )
    token = dev_token(
        api.settings, subject=t.admin_subject, audience=AUDIENCE, issuer="https://idp.example/t"
    )
    assert (await _me(api, t.subdomain, token)).status_code == 200
    assert (await _me(api, t.subdomain, token)).status_code == 200
    assert len(calls) == 1  # cached
    rotated = jwt.encode(
        {
            "iss": "https://idp.example/t",
            "sub": t.admin_subject,
            "aud": AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        dev_private_key(api.settings),
        algorithm="RS256",
        headers={"kid": "rotated-unknown"},
    )
    assert (
        await _me(api, t.subdomain, rotated)
    ).status_code == 401  # unknown kid even after refresh
    assert len(calls) <= 2  # at most one refresh attempt


def test_dev_idp_is_refused_outside_disposable_environments() -> None:
    from pydantic import ValidationError

    from edisc_core.settings import Environment, Settings

    with pytest.raises(ValidationError, match="EDISC_API_DEV_IDP"):
        Settings(_env_file=None, env=Environment.PRODUCTION, api_dev_idp=True)  # type: ignore[call-arg]
