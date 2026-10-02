"""API tests: the real app over ASGI with real Postgres, MinIO, Redis and Temporal (nothing mocked
except the external identity provider's JWKS endpoint where a test needs an HTTP IdP)."""

from __future__ import annotations

import contextlib
import ipaddress
import secrets
import socket
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker
from types_aiobotocore_s3 import S3Client

from edisc_api.admin import onboard_tenant
from edisc_api.app import Resources, create_app
from edisc_api.auth import DEV_ISSUER, DEV_JWKS, Authenticator, JwksCache, dev_token
from edisc_connector_dummy.connector import DummyConnector
from edisc_connector_slack_export import file_links
from edisc_connector_slack_export.connector import SlackExportConnector
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.envelope import SecretBox
from edisc_core.ids import new_id
from edisc_core.kms import LocalKmsClient
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.activities import Activities
from edisc_worker.contracts import EXPORTS_QUEUE, task_queue
from edisc_worker.exports import ExportActivities
from edisc_worker.workflows import CollectionJobWorkflow, CollectUnitWorkflow, ExportIngestWorkflow

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
            # the auth tests produce many 401s from the test client's one address; the throttling test
            # lowers this itself
            "api_auth_failures_per_minute": 10_000,
            # jobs started through the API take their RunConfig from settings. The poll stays at the
            # production 120 s on purpose: every job test then fails (times out) if a finished child's
            # signal is lost and the parent waits for the poll ("keep-early-wake" patch regression)
            "job_poll_seconds": 120,
            "activity_retry_initial_seconds": 0.1,
            "activity_retry_max_seconds": 0.5,
            "activity_max_attempts": 4,
            "activity_heartbeat_timeout_seconds": 10,
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
    redis = aioredis.from_url(api_settings.redis_url)
    resources = Resources(
        api_settings,
        app_sessions,
        s3,
        temporal,
        limiter,
        SecretBox(kms),
        Authenticator(api_settings, app_sessions, JwksCache(api_settings, http)),
        {"dummy": DummyConnector(limiter)},
        redis,
    )
    yield Api(api_settings, app_sessions, s3, temporal, resources, http)
    await http.aclose()
    await redis.aclose()


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


# ------------------------------------------------------------------ Slack exports (ADR 0014)
MiB = 1 << 20


def export_settings(api: Api, **overrides: Any) -> Settings:
    return api.settings.model_copy(
        update={
            "export_upload_part_min_bytes": 5 * MiB,
            "export_complete_wait_seconds": 20,
            "export_read_window_bytes": 1 * MiB,
            "export_file_hosts": ["files.dummy.test"],
            "file_retry_backoff_seconds": 0.01,
            **overrides,
        }
    )


@contextlib.asynccontextmanager
async def export_worker(api: Api, **overrides: Any) -> AsyncIterator[Api]:
    """The API with export settings, and the ``exports`` worker those settings drive."""
    settings = export_settings(api, **overrides)
    api.resources.settings = settings
    tuned = Api(settings, api.sessions, api.s3, api.temporal, api.resources, api.http)
    acts = ExportActivities(api.sessions, api.s3, settings)
    async with Worker(
        api.temporal,
        task_queue=EXPORTS_QUEUE,
        workflows=[ExportIngestWorkflow],
        activities=acts.all(),
    ):
        yield tuned


@pytest.fixture
async def exp(api: Api) -> AsyncIterator[Api]:
    async with export_worker(
        api, export_entry_batch=3
    ) as tuned:  # several batches even for small zips
        yield tuned


@pytest.fixture
async def exp_batched(api: Api) -> AsyncIterator[Api]:
    """Production batch size (large synthetic exports)."""
    async with export_worker(api) as tuned:
        yield tuned


class FileHost:
    """The file host behind export links (an external service, like the IdP's JWKS endpoint, so it is
    the one thing faked here). ``outcomes`` maps a file id to an HTTP status or an exception; anything
    else is served from the dummy dataset."""

    def __init__(self, dataset: Any = None) -> None:
        self.dataset = dataset
        self.outcomes: dict[str, int | Exception] = {}
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        file_id = request.url.path.strip("/").split("/")[0]
        self.requests.append(file_id)
        outcome = self.outcomes.get(file_id)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return httpx.Response(outcome, text="refused")
        if self.dataset is None:
            return httpx.Response(404)
        return httpx.Response(200, content=self.dataset.file_bytes(file_id))


FILE_HOST_IP = ipaddress.ip_address(
    "93.184.215.14"
)  # any global address: never dialled (MockTransport)


async def fake_dns(host: str, port: int) -> list[file_links.IPAddress]:
    """Export file hosts are fake (``.test``); links resolve to a global address."""
    if host.endswith(".test"):
        return [FILE_HOST_IP]
    raise socket.gaierror(socket.EAI_NONAME, "unknown host")


def export_connector(api: Api, host: FileHost) -> SlackExportConnector:
    http = httpx.AsyncClient(transport=httpx.MockTransport(host.handler))
    return SlackExportConnector(
        api.sessions, api.s3, api.settings, api.resources.limiter, http, resolver=fake_dns
    )


@contextlib.asynccontextmanager
async def collection_workers(api: Api, host: FileHost) -> AsyncIterator[Api]:
    """Export upload/validation AND collection workers, plus the dummy (live API) worker, all sharing
    the export settings; the API resources get the same connectors."""
    async with export_worker(api) as tuned:
        connectors = {
            "dummy": DummyConnector(tuned.resources.limiter),
            "slack_export": export_connector(tuned, host),
        }
        tuned.resources.connectors = connectors
        acts = Activities(
            tuned.sessions,
            tuned.s3,
            tuned.settings.model_copy(update={"activity_time_box_seconds": 30}),
            connectors,
            tuned.temporal,
        )
        async with (
            Worker(
                tuned.temporal,
                task_queue=task_queue("slack_export"),
                workflows=[CollectionJobWorkflow, CollectUnitWorkflow],
                activities=acts.all(),
            ),
            Worker(
                tuned.temporal,
                task_queue=task_queue("dummy"),
                workflows=[CollectionJobWorkflow, CollectUnitWorkflow],
                activities=acts.all(),
            ),
        ):
            yield tuned
