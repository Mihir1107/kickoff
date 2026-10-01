"""API tests: the real app over ASGI with real Postgres, MinIO, Redis and Temporal (nothing mocked
except the external identity provider's JWKS endpoint where a test needs an HTTP IdP)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

from edisc_api.admin import onboard_tenant
from edisc_api.app import Resources, create_app
from edisc_api.auth import DEV_ISSUER, DEV_JWKS, Authenticator, JwksCache, dev_token
from edisc_connector_dummy.connector import DummyConnector
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.envelope import SecretBox
from edisc_core.ids import new_id
from edisc_core.kms import LocalKmsClient
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx

Sessions = async_sessionmaker[AsyncSession]
AUDIENCE = "edisc-api"

# Every credential a test hands to the API. Every response of every API test is scanned for them
# (ADR 0013 section 5): a hit fails the test that made the request.
SECRETS: set[str] = set()


def secret(prefix: str = "xoxb") -> str:
    value = f"{prefix}-canary-{secrets.token_hex(12)}"
    SECRETS.add(value)
    return value


async def _scan_response(response: httpx.Response) -> None:
    body = (await response.aread()).decode(errors="replace")
    leaked = [s for s in SECRETS if s in body or s in str(response.headers)]
    assert not leaked, f"{response.request.method} {response.request.url} leaked a credential"


@pytest.fixture(scope="session")
def api_settings(settings: Settings, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return settings.model_copy(
        update={
            "api_dev_idp": True,
            "local_kms_dir": Path(tmp_path_factory.mktemp("api-kms")),
        }
    )


@pytest.fixture(scope="session")
def kms(api_settings: Settings) -> LocalKmsClient:
    return LocalKmsClient(api_settings)


@dataclass
class Api:
    settings: Settings
    sessions: Sessions
    s3: S3Client
    temporal: Client
    resources: Resources
    http: httpx.AsyncClient  # used by the JWKS cache (tests may swap its transport)

    def client(self, subdomain: str, token: str | None = None) -> httpx.AsyncClient:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.settings, self.resources)),
            base_url=f"http://{subdomain}.{self.settings.api_base_domain}",
            headers=headers,
            event_hooks={"response": [_scan_response]},
        )


@pytest.fixture
async def api(
    api_settings: Settings,
    app_sessions: Sessions,
    s3: S3Client,
    temporal: Client,
    limiter: RateLimiter,
    kms: LocalKmsClient,
) -> AsyncIterator[Api]:
    http = httpx.AsyncClient()
    resources = Resources(
        api_settings,
        app_sessions,
        s3,
        temporal,
        limiter,
        SecretBox(kms),
        Authenticator(api_settings, app_sessions, JwksCache(api_settings, http)),
        {"dummy": DummyConnector(limiter)},
    )
    yield Api(api_settings, app_sessions, s3, temporal, resources, http)
    await http.aclose()


@dataclass
class TenantCtx:
    tenant_id: uuid.UUID
    subdomain: str
    admin_subject: str
    admin_id: uuid.UUID
    default_client_id: uuid.UUID

    def token(self, settings: Settings, subject: str | None = None, **claims: Any) -> str:
        return dev_token(
            settings, subject=subject or self.admin_subject, audience=AUDIENCE, **claims
        )


async def new_api_tenant(
    api: Api, kms: LocalKmsClient, *, issuer: str = DEV_ISSUER, jwks_url: str = DEV_JWKS
) -> TenantCtx:
    sub = f"t{secrets.token_hex(5)}"
    key_ref = f"tenant/{sub}"
    kms.create_key(key_ref)
    admin_subject = f"admin-{secrets.token_hex(4)}"
    done = await onboard_tenant(
        api.sessions,
        kms_key_ref=key_ref,
        subdomain=sub,
        name=f"Tenant {sub}",
        issuer=issuer,
        audience=AUDIENCE,
        jwks_url=jwks_url,
        admin_subject=admin_subject,
        admin_name="Admin",
        operator="tests",
    )
    return TenantCtx(
        done.tenant_id, sub, admin_subject, done.admin_principal_id, done.default_client_id
    )


async def add_principal(
    api: Api,
    t: TenantCtx,
    *,
    roles: list[tuple[str, str, uuid.UUID | None]] = (),  # type: ignore[assignment]
    issuer: str = DEV_ISSUER,
    kind: str = "user",
) -> tuple[uuid.UUID, str]:
    """A principal with role assignments (role, scope_type, scope_id). Returns (id, subject)."""
    pid, subject = new_id(), f"user-{secrets.token_hex(4)}"
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind, issuer, subject, display_name)"
                " VALUES (:p, :t, :k, :i, :s, 'U')"
            ),
            {"p": pid, "t": t.tenant_id, "k": kind, "i": issuer, "s": subject},
        )
        for role, scope_type, scope_id in roles:
            await s.execute(
                text(
                    "INSERT INTO role_assignments (id, tenant_id, principal_id, role, scope_type, scope_id, created_by)"
                    " VALUES (:i, :t, :p, :r, :st, :sid, 'tests')"
                ),
                {
                    "i": new_id(),
                    "t": t.tenant_id,
                    "p": pid,
                    "r": role,
                    "st": scope_type,
                    "sid": scope_id,
                },
            )
    return pid, subject


@pytest.fixture
async def tenant(api: Api, kms: LocalKmsClient) -> TenantCtx:
    return await new_api_tenant(api, kms)
