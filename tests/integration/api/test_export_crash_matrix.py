"""M14.7: the crash matrix during Slack export ingestion (ADR 0014; principle 5, resumability).

- Upload -> lock -> validate: a crash at every boundary (simulated like SIGKILL: nothing after the point
  runs, open transactions roll back), then a fresh run from the database alone, must end exactly like a
  run that never crashed: same findings, same directory/day-file/thread rows, one connection, one
  ``export_uploaded`` and one ``export_validated`` audit event, the zip locked once, staging gone.
- Collection from the export: a crash at every pipeline boundary resumes to oracle-exact results
  (every message once, every unit matched against the archive, entries referenced once, chain valid).
- A real SIGKILL of the export worker PROCESS in the middle of validation, then a new worker.
"""

from __future__ import annotations

import asyncio
import io
import os
import signal
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text

from edisc_connector_dummy.connector import scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportOptions, write_export
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import Connection
from edisc_core.ids import new_id
from edisc_core.schemas import JobStatus
from edisc_custody.log import verify_chain
from edisc_db.session import tenant_tx
from edisc_worker.exports import ExportIngest
from edisc_worker.pipeline import CrashHooks, Pipeline

from ..pipeline.conftest import CrashAt, SimulatedCrash
from .conftest import Api, FileHost, TenantCtx, export_connector, export_worker
from .test_exports import put_part
from .test_jobs import make_world

SPEC = DatasetSpec(
    seed=41, dialect="slack_history", conversations=4, days=3, messages_per_unit=12, p_file=0.2
)
ROOT = Path(__file__).resolve().parents[3]


def export_bytes(spec: DatasetSpec = SPEC) -> bytes:
    buf = io.BytesIO()
    write_export(Dataset(spec), buf, ExportOptions())
    return buf.getvalue()


async def stage(api: Api, t: TenantCtx, client: uuid.UUID, data: bytes) -> uuid.UUID:
    """Uploaded and completed as far as the API goes; the workflow's steps are driven by the test."""
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        export_id = (
            await c.post(f"/v1/clients/{client}/exports", json={"size_bytes": len(data)})
        ).json()["id"]
        assert (await put_part(c, export_id, 1, data)).status_code == 200
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(
            text("UPDATE slack_exports SET status = 'locking' WHERE id = :i"),
            {"i": uuid.UUID(export_id)},
        )
    return uuid.UUID(export_id)


async def ingest(
    api: Api, t: TenantCtx, export_id: uuid.UUID, hooks: CrashHooks | None = None
) -> str:
    worker = ExportIngest(api.sessions, api.s3, api.settings, hooks)
    status = (await worker.lock(t.tenant_id, export_id))["status"]
    if status == "validating":
        status = (await worker.validate(t.tenant_id, export_id))["status"]
    return str(status)


async def state(api: Api, t: TenantCtx, export_id: uuid.UUID) -> dict[str, Any]:
    """Everything a resumed ingestion must reproduce exactly."""
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, findings, entry_count, detected_tier, workspace_id, sha256,"
                    " evidence_object_id, staging_key FROM slack_exports WHERE id = :i"
                ),
                {"i": export_id},
            )
        ).one()

        async def count(sql: str) -> int:
            return int((await s.execute(text(sql), {"e": export_id})).scalar_one())

        counts = {
            "entries": await count("SELECT count(*) FROM export_entries WHERE export_id = :e"),
            "day_files": await count("SELECT count(*) FROM export_day_files WHERE export_id = :e"),
            "threads": await count("SELECT count(*) FROM export_threads WHERE export_id = :e"),
            "conversations": await count(
                "SELECT count(*) FROM export_conversations WHERE export_id = :e"
            ),
            "connections": await count(
                "SELECT count(*) FROM connections WHERE config->>'export_id' = CAST(CAST(:e AS uuid) AS text)"
            ),
            "uploaded_events": await count(
                "SELECT count(*) FROM custody_events WHERE event_type = 'audit.export_uploaded'"
                " AND payload->>'export_id' = CAST(CAST(:e AS uuid) AS text)"
            ),
            "validated_events": await count(
                "SELECT count(*) FROM custody_events WHERE event_type = 'audit.export_validated'"
                " AND payload->>'export_id' = CAST(CAST(:e AS uuid) AS text)"
            ),
        }
        locked = (
            await s.execute(
                text("SELECT count(*) FROM evidence_objects WHERE sha256 = :h AND kind = 'file'"),
                {"h": row.sha256},
            )
        ).scalar_one()
    try:
        await api.s3.head_object(Bucket=api.settings.s3_staging_bucket, Key=row.staging_key)
        staging_left = True
    except ClientError:
        staging_left = False
    findings = {k: v for k, v in (row.findings or {}).items() if k != "range_requests"}
    return {
        "status": row.status,
        "findings": findings,
        "entry_count": row.entry_count,
        "tier": row.detected_tier,
        "workspace": row.workspace_id,
        "sha256": row.sha256,
        "zip_rows": locked,
        "staging_left": staging_left,
        **counts,
    }


INGEST_POINTS = [
    ("lock:after_complete", 1),
    ("lock:after_hash", 1),
    ("lock:after_evidence", 1),
    ("lock:after_commit", 1),
    ("validate:after_entries_batch", 1),
    ("validate:after_entries_batch", 4),
    ("validate:after_conversations", 1),
    ("validate:after_day_files", 1),
    ("validate:before_ready", 1),
    ("validate:after_ready", 1),
]


@pytest.mark.parametrize(("point", "nth"), INGEST_POINTS)
async def test_ingestion_crash_matrix_resumes_to_the_clean_result(
    api: Api, tenant: TenantCtx, point: str, nth: int
) -> None:
    data = export_bytes()
    async with export_worker(api, export_entry_batch=3) as exp:  # many directory batches
        clean = await stage(exp, tenant, tenant.default_client_id, data)
        assert await ingest(exp, tenant, clean) == "ready"
        crashed = await stage(exp, tenant, tenant.default_client_id, data)
        with pytest.raises(SimulatedCrash):
            await ingest(exp, tenant, crashed, CrashAt(point, nth))
        assert await ingest(exp, tenant, crashed) == "ready"  # a fresh "process", from the DB alone
        assert await ingest(exp, tenant, crashed) == "ready"  # and once more: nothing changes
        expected, got = await state(exp, tenant, clean), await state(exp, tenant, crashed)
    assert got == expected, (point, nth)
    assert got["status"] == "ready" and got["connections"] == 1
    assert (got["uploaded_events"], got["validated_events"]) == (1, 1)
    assert got["zip_rows"] == 1 and not got["staging_left"]  # locked once (dedup), staging deleted


# ------------------------------------------------------------------ collection from the export
COLLECT_POINTS = ["after_evidence", "mid_transaction", "after_commit", "during_finalize"]


@pytest.mark.parametrize("nth", [1, 4])
@pytest.mark.parametrize("point", COLLECT_POINTS)
async def test_collection_crash_matrix_resumes_to_oracle_exact_results(
    api: Api, tenant: TenantCtx, point: str, nth: int
) -> None:
    ds = Dataset(SPEC)
    async with export_worker(api) as exp:
        w = await make_world(exp, tenant, SPEC)
        export_id = await stage(exp, tenant, uuid.UUID(w.client), export_bytes())
        assert await ingest(exp, tenant, export_id) == "ready"
        async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT c.id, c.external_org_id FROM slack_exports x"
                        " JOIN connections c ON c.id = x.connection_id WHERE x.id = :i"
                    ),
                    {"i": export_id},
                )
            ).one()
        conn = Connection(
            tenant.tenant_id, row.id, "slack_export", row.external_org_id,
            {"export_id": str(export_id)},
        )  # fmt: skip
        scope = scope_for_days(
            "*", datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC), ds.n_days(0)
        )
        job_id = new_id()

        def pipeline(hooks: CrashHooks | None) -> Pipeline:
            return Pipeline(
                exp.sessions, exp.s3, exp.settings, export_connector(exp, FileHost(ds)),
                hooks or CrashHooks(),
            )  # fmt: skip

        async def run(p: Pipeline) -> JobStatus:
            await p.start_job(
                tenant_id=tenant.tenant_id, job_id=job_id, matter_id=uuid.UUID(w.matter),
                connection_id=row.id, scopes=[scope], requested_by="tester",
            )  # fmt: skip
            return await p.run(tenant_id=tenant.tenant_id, job_id=job_id, conn=conn)

        with pytest.raises(SimulatedCrash):
            await run(pipeline(CrashAt(point, nth)))
        status = await run(pipeline(None))  # a fresh "process": new connector, new pipeline

        expected = {
            f"{SPEC.workspace_id}/{c.id}/{m.ts}"
            for c in ds.conversations()
            for d in range(ds.n_days(0))
            for m in ds.unit_messages(c.id, d, 0)
            if m.deleted_ts is None
        }
        async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
            linked = set(
                (
                    await s.execute(
                        text(
                            "SELECT i.source_item_id FROM job_items ji JOIN items i ON i.id = ji.item_id"
                            " WHERE ji.job_id = :j AND i.item_type = 'message' AND ji.in_scope"
                        ),
                        {"j": job_id},
                    )
                ).scalars()
            )
            bad_units = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM work_units WHERE job_id = :j AND kind = 'conversation_day'"
                        " AND (status <> 'done' OR recon_status <> 'matched_against_archive'"
                        " OR archive_accounted IS DISTINCT FROM expected_count)"
                    ),
                    {"j": job_id},
                )
            ).scalar_one()
            dupes = (
                await s.execute(
                    text("SELECT count(*) - count(DISTINCT idempotency_key) FROM items")
                )
            ).scalar_one()
            versions = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM (SELECT source_item_id FROM items WHERE item_type ="
                        " 'message' GROUP BY source_item_id HAVING count(*) > 1) q"
                    )
                )
            ).scalar_one()
            pending = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM evidence_objects WHERE job_id = :j AND state <> 'complete'"
                    ),
                    {"j": job_id},
                )
            ).scalar_one()
            entries, distinct_entries = (
                await s.execute(
                    text(
                        "SELECT count(*), count(DISTINCT entry_path) FROM evidence_objects"
                        " WHERE kind = 'archive_entry' AND archive_evidence_id ="
                        " (SELECT evidence_object_id FROM slack_exports WHERE id = :x)"
                    ),
                    {"x": export_id},
                )
            ).one()
        report = await verify_chain(
            exp.sessions, exp.s3, exp.settings, tenant_id=tenant.tenant_id, stream_id=job_id
        )
    assert status is JobStatus.COMPLETED_AGAINST_ARCHIVE, (point, nth)
    assert linked == expected
    assert (bad_units, dupes, versions, pending) == (0, 0, 0, 0)
    day_files = SPEC.conversations * SPEC.days
    assert entries == distinct_entries == day_files + 1  # every day file + users.json, once each
    assert report.ok, report.errors


# ------------------------------------------------------------------ a real SIGKILL
async def test_sigkill_of_the_export_worker_during_validation_resumes_exactly(
    api: Api, tenant: TenantCtx, tmp_path: Path
) -> None:
    """The export worker is a separate PROCESS, SIGKILLed while it writes the day-file index; a new
    worker process finishes. The result equals the archive exactly and every audit event exists once."""
    spec = DatasetSpec(seed=43, conversations=40, days=60, messages_per_unit=12, p_file=0.0)
    data = export_bytes(spec)
    exp = export_settings_api(api)
    env = {**os.environ, "EDISC_EXPORT_ENTRY_BATCH": "200"}

    async def spawn(n: int) -> asyncio.subprocess.Process:
        log = (tmp_path / f"worker-{n}.log").open("wb")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "edisc_worker", "--source", "slack_export", "--exports",
            "--queue", f"collect-crash-{uuid.uuid4().hex[:8]}",
            cwd=ROOT, env=env, stdout=log, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        log.close()
        return proc

    worker = await spawn(1)
    try:
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            r = await c.post(
                f"/v1/clients/{tenant.default_client_id}/exports", json={"size_bytes": len(data)}
            )
            export_id = r.json()["id"]
            step = 5 << 20
            for n, start in enumerate(range(0, len(data), step), start=1):
                assert (
                    await put_part(c, export_id, n, data[start : start + step])
                ).status_code == 200
            assert (await c.post(f"/v1/exports/{export_id}/complete")).status_code in (202, 422)
            eid = uuid.UUID(export_id)
            async with asyncio.timeout(120):  # kill mid-way through the day-file index
                while True:
                    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
                        indexed = (
                            await s.execute(
                                text("SELECT count(*) FROM export_day_files WHERE export_id = :e"),
                                {"e": eid},
                            )
                        ).scalar_one()
                    if indexed > 0:
                        break
                    await asyncio.sleep(0.05)
            worker.send_signal(signal.SIGKILL)
            await worker.wait()
            assert (await c.get(f"/v1/exports/{export_id}")).json()["status"] == "validating"
            worker = await spawn(2)
            async with asyncio.timeout(240):
                while True:
                    out = (await c.get(f"/v1/exports/{export_id}")).json()
                    if out["status"] in ("ready", "rejected"):
                        break
                    await asyncio.sleep(0.5)
    finally:
        if worker.returncode is None:
            worker.send_signal(signal.SIGKILL)
            await worker.wait()
    got = await state(exp, tenant, eid)
    assert got["status"] == "ready", out
    assert got["day_files"] == spec.conversations * spec.days
    assert got["entries"] == out["entry_count"]
    assert (got["uploaded_events"], got["validated_events"], got["connections"]) == (1, 1, 1)
    assert got["findings"]["messages"] == sum(
        len([m for m in Dataset(spec).unit_messages(cv.id, d, 0) if m.deleted_ts is None])
        for cv in Dataset(spec).conversations()
        for d in range(spec.days)
    )


def export_settings_api(api: Api) -> Api:
    from .conftest import export_settings

    settings = export_settings(api)
    api.resources.settings = settings
    return Api(settings, api.sessions, api.s3, api.temporal, api.resources, api.http)
