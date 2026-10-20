"""Per-tenant Slack export upload quotas (M18 §4, D10; threat T13), through the real API.

Usage is computed from the export rows under a per-tenant lock in the create transaction, so N
concurrent creates at the limit admit exactly the allowed number; a rejected or expired upload
stops counting; another tenant's uploads never count; the 409 names the limit and the usage, never
the size the request declared.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_api.quota_admin import QuotaRefusedError, set_quota
from edisc_api.quotas import Quota
from edisc_core.kms import LocalKmsClient
from edisc_core.settings import Settings
from edisc_db.session import create_engine, session_factory, tenant_tx, warm_pool
from edisc_worker.export_expiry import expire_export_uploads

from .conftest import Api, TenantCtx, new_api_tenant
from .test_exports import audit_events, create

GiB = 1 << 30
Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
async def owner_sessions(settings: Settings, migrated: None) -> AsyncIterator[Sessions]:
    """The operator CLI's credentials (the migration owner)."""
    engine = create_engine(settings, "owner", pool_size=2)
    await warm_pool(engine)
    yield session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def other_tenant(api: Api, kms: LocalKmsClient) -> TenantCtx:
    return await new_api_tenant(api, kms)


async def _quota(api: Api, t: TenantCtx, owner_sessions: Sessions, **kw: int) -> None:
    q = {"export_bytes_in_flight": 100 * GiB, "export_bytes_stored": 1000 * GiB,
         "export_uploads_concurrent": 3, **kw}  # fmt: skip
    await set_quota(owner_sessions, tenant_id=t.tenant_id, quota=Quota(**q),
                    reason="test", operator="tests")  # fmt: skip


async def test_concurrent_creates_at_the_limit_admit_exactly_the_allowed_number(
    exp: Api, tenant: TenantCtx, owner_sessions: Sessions
) -> None:
    await _quota(exp, tenant, owner_sessions, export_uploads_concurrent=3)
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        answers = await asyncio.gather(
            *(create(c, tenant.default_client_id, 10) for _ in range(12))
        )
        codes = sorted(r.status_code for r in answers)
        assert codes == [201] * 3 + [409] * 9, codes
        refused = [r for r in answers if r.status_code == 409]
        assert {r.json()["error"] for r in refused} == {"quota_exceeded"}
        assert "export_uploads_concurrent: limit 3, in use 3" in refused[0].json()["detail"]
        got = (await c.get("/v1/tenant/quotas")).json()
        assert got["limits"]["export_uploads_concurrent"] == 3
        assert got["usage"] == {"export_bytes_in_flight": 30, "export_bytes_stored": 30,
                                "export_uploads_concurrent": 3}  # fmt: skip
    events = await audit_events(exp, tenant, "audit.export_quota_refused")
    assert len(events) == 9
    # nothing staged is left behind for a refused create: only the 3 accepted uploads are open
    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        assert (await s.execute(text("SELECT count(*) FROM slack_exports"))).scalar_one() == 3
    open_uploads, kw = [], {}
    while True:
        page = await exp.s3.list_multipart_uploads(Bucket=exp.settings.s3_staging_bucket, **kw)
        open_uploads += [u["Key"] for u in page.get("Uploads", [])
                         if u["Key"].startswith(f"exports/{tenant.tenant_id}/")]  # fmt: skip
        if not page.get("IsTruncated"):
            break
        kw = {"KeyMarker": page["NextKeyMarker"], "UploadIdMarker": page["NextUploadIdMarker"]}
    assert len(open_uploads) == 3, open_uploads


async def test_byte_quotas_and_no_echo_of_the_declared_size(
    exp: Api, tenant: TenantCtx, owner_sessions: Sessions
) -> None:
    await _quota(exp, tenant, owner_sessions, export_bytes_in_flight=1000, export_bytes_stored=1500)
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        assert (await create(c, tenant.default_client_id, 600)).status_code == 201
        r = await create(c, tenant.default_client_id, 987_654)  # a recognisable declared size
        assert r.status_code == 409, r.text
        assert "987654" not in r.text and "987,654" not in r.text
        assert "export_bytes_in_flight: limit 1000, in use 600" in r.json()["detail"]
        assert (await create(c, tenant.default_client_id, 400)).status_code == 201  # exactly at it
        assert (await create(c, tenant.default_client_id, 1)).status_code == 409


async def test_quotas_are_per_tenant(
    exp: Api, tenant: TenantCtx, other_tenant: TenantCtx, owner_sessions: Sessions
) -> None:
    await _quota(exp, tenant, owner_sessions, export_uploads_concurrent=1)
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        assert (await create(c, tenant.default_client_id, 10)).status_code == 201
        assert (await create(c, tenant.default_client_id, 10)).status_code == 409
    async with exp.client(other_tenant.subdomain, other_tenant.token(exp.settings)) as c:
        # the other tenant has the defaults and none of this tenant's usage
        for _ in range(exp.settings.export_quota_uploads_concurrent):
            assert (await create(c, other_tenant.default_client_id, 10)).status_code == 201
        assert (await create(c, other_tenant.default_client_id, 10)).status_code == 409


async def test_an_expired_upload_releases_its_quota(
    exp: Api, tenant: TenantCtx, owner_sessions: Sessions, sweeper_sessions: Sessions
) -> None:
    await _quota(exp, tenant, owner_sessions, export_uploads_concurrent=1)
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        first = (await create(c, tenant.default_client_id, 10)).json()["id"]
        assert (await create(c, tenant.default_client_id, 10)).status_code == 409
        # not yet past its TTL: the sweep leaves it alone
        none = await expire_export_uploads(
            sweeper_sessions, exp.sessions, exp.s3, exp.settings, tenant_id=tenant.tenant_id
        )
        assert none.expired == []
        done = await expire_export_uploads(
            sweeper_sessions, exp.sessions, exp.s3, exp.settings, tenant_id=tenant.tenant_id,
            ttl=timedelta(0),
        )  # fmt: skip
        assert done.expired == [uuid.UUID(first)]
        out = (await c.get(f"/v1/exports/{first}")).json()
        assert (out["status"], out["reject_reason"]) == ("rejected", "upload_expired")
        assert (await create(c, tenant.default_client_id, 10)).status_code == 201
    assert [
        e[2]["export_id"] for e in await audit_events(exp, tenant, "audit.export_upload_expired")
    ] == [first]


async def test_a_rejected_export_releases_its_quota(
    exp: Api, tenant: TenantCtx, owner_sessions: Sessions
) -> None:
    from .test_exports import settled, slack_export, upload

    await _quota(exp, tenant, owner_sessions, export_uploads_concurrent=1)
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id, done = await upload(c, tenant.default_client_id, slack_export(), sha256="0" * 64)
        # R7: the declared hash differs: rejected at completion, before any parsing
        assert (done.status_code, done.json()["error"]) == (422, "declared_hash_mismatch")
        assert (await settled(c, export_id))["status"] == "rejected"
        assert (await create(c, tenant.default_client_id, 10)).status_code == 201


async def test_only_the_operator_sets_quotas(
    exp: Api, tenant: TenantCtx, owner_sessions: Sessions
) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):  # the app role: read only
        async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
            await s.execute(text(
                "INSERT INTO tenant_quotas (tenant_id, export_bytes_in_flight, export_bytes_stored,"
                " export_uploads_concurrent) VALUES (:t, 1, 1, 1)"), {"t": tenant.tenant_id})  # fmt: skip
    with pytest.raises(QuotaRefusedError):
        await _quota(exp, tenant, owner_sessions, export_uploads_concurrent=0)
    await _quota(exp, tenant, owner_sessions, export_uploads_concurrent=7)
    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        change = (await s.execute(text(
            "SELECT operator, before, after FROM tenant_quota_changes ORDER BY created_at DESC LIMIT 1"
        ))).one()  # fmt: skip
    assert change.operator == "operator:tests" and change.after["export_uploads_concurrent"] == 7
    assert [e[0] for e in await audit_events(exp, tenant, "audit.export_quota_set")]
