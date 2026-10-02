"""Reopening (ADR 0002, 2026-10-02 review): tenant admins only, audited; evidence whose retention lapsed
while closed is re-locked if it still exists, and every unprotected window is recorded and readable."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.worker import Worker

from edisc_core.time import ensure_utc, utc_now
from edisc_db.session import tenant_tx
from edisc_worker.contracts import MAINTENANCE_QUEUE
from edisc_worker.maintenance import MaintenanceActivities
from edisc_worker.workflows import TenantRetentionWorkflow

from .conftest import Api, TenantCtx, add_principal, export_worker
from .test_exports import settled, slack_export, upload


async def _evidence(api: Api, t: TenantCtx, export_id: str) -> tuple[str, str, datetime]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT e.storage_key, e.version_id, e.retain_until FROM slack_exports x"
                    " JOIN evidence_objects e ON e.id = x.evidence_object_id WHERE x.id = :i"
                ),
                {"i": uuid.UUID(export_id)},
            )
        ).one()
    return row.storage_key, row.version_id, ensure_utc(row.retain_until)


async def test_reopen_relocks_what_lapsed_and_records_every_gap(
    api: Api, tenant: TenantCtx, sweeper_sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with export_worker(api, evidence_retention_override_seconds=3) as exp:
        maint = MaintenanceActivities(sweeper_sessions, exp.sessions, exp.s3, exp.settings)
        async with (
            Worker(
                exp.temporal,
                task_queue=MAINTENANCE_QUEUE,
                workflows=[TenantRetentionWorkflow],
                activities=maint.all(),
            ),
            exp.client(tenant.subdomain, tenant.token(exp.settings)) as c,
        ):
            client = (await c.post("/v1/clients", json={"name": "Reopened"})).json()["id"]
            ids = []
            for n in (1, 2):
                export_id, _ = await upload(
                    c, uuid.UUID(client), slack_export(extra={"note.txt": str(n).encode()})
                )
                assert (await settled(c, export_id))["status"] == "ready"
                ids.append(export_id)
            assert (await c.post(f"/v1/clients/{client}/close")).status_code == 200

            kept, gone = [await _evidence(exp, tenant, i) for i in ids]
            lapse = max(kept[2], gone[2])
            await asyncio.sleep(max(0.0, (lapse - utc_now()).total_seconds()) + 1.5)
            # unprotected while closed: someone deletes one of them
            await exp.s3.delete_object(
                Bucket=exp.settings.s3_evidence_bucket, Key=gone[0], VersionId=gone[1]
            )

            _, client_admin = await add_principal(
                exp, tenant, roles=[("client_admin", "client", uuid.UUID(client))]
            )
            async with exp.client(
                tenant.subdomain, tenant.token(exp.settings, subject=client_admin)
            ) as other:
                assert (await other.post(f"/v1/clients/{client}/reopen")).status_code == 403

            reopened_at = datetime.now(UTC)
            r = await c.post(f"/v1/clients/{client}/reopen")
            assert r.status_code == 200 and r.json()["closed_at"] is None
            assert (await c.post(f"/v1/clients/{client}/reopen")).status_code == 409
            async with asyncio.timeout(30):
                while True:
                    gaps = (await c.get(f"/v1/clients/{client}/retention-gaps")).json()["items"]
                    if len(gaps) == 2:
                        break
                    await asyncio.sleep(0.3)

    by_outcome = {g["outcome"]: g for g in gaps}
    assert set(by_outcome) == {"relocked", "missing"}
    for outcome, (_, _, lapsed) in (("relocked", kept), ("missing", gone)):
        g = by_outcome[outcome]
        assert ensure_utc(datetime.fromisoformat(g["unprotected_from"])) == lapsed
        assert ensure_utc(datetime.fromisoformat(g["unprotected_until"])) >= reopened_at
        assert (g["owner_type"], g["owner_id"]) == ("client", client)
    retention = await api.s3.get_object_retention(
        Bucket=api.settings.s3_evidence_bucket, Key=kept[0], VersionId=kept[1]
    )
    assert ensure_utc(retention["Retention"]["RetainUntilDate"]) > reopened_at  # locked again
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        events = (
            await s.execute(
                text(
                    "SELECT event_type, payload FROM custody_events WHERE stream_id = :t"
                    " AND event_type IN ('audit.client_reopened', 'audit.retention_gap') ORDER BY seq"
                ),
                {"t": tenant.tenant_id},
            )
        ).all()
        alerts = (
            await s.execute(text("SELECT count(*) FROM alerts WHERE kind = 'evidence_missing'"))
        ).scalar_one()
    assert [e.event_type for e in events] == ["audit.client_reopened", "audit.retention_gap"]
    assert (events[1].payload["relocked"], events[1].payload["missing"]) == (1, 1)
    assert alerts == 1


async def test_matter_reopen_rules(api: Api, tenant: TenantCtx) -> None:
    from .test_jobs import make_world

    w = await make_world(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        assert (await c.post(f"/v1/matters/{w.matter}/reopen")).status_code == 409  # not closed
        assert (await c.post(f"/v1/matters/{w.matter}/close")).status_code == 200
        assert (await c.post(f"/v1/clients/{w.client}/close")).status_code == 200
        r = await c.post(f"/v1/matters/{w.matter}/reopen")
        assert r.status_code == 409 and r.json()["detail"] == "the client is closed"
        assert (await c.post(f"/v1/clients/{w.client}/reopen")).status_code == 200
        r = await c.post(
            f"/v1/matters/{w.matter}/reopen", json={"retention_until": "2001-01-01T00:00:00Z"}
        )
        assert r.status_code == 422  # never shortened
        r = await c.post(f"/v1/matters/{w.matter}/reopen")
        assert r.status_code == 200 and r.json()["closed_at"] is None
        assert (await c.get(f"/v1/matters/{w.matter}/retention-gaps")).json()["items"] == []
