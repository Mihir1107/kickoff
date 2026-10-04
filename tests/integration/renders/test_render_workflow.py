"""M15 step 4: the render workflow and the render's own custody stream (ADR 0015 §14), on the real
stack (Temporal, Postgres, MinIO with Object Lock). Nothing mocked.

- a render of a sealed job runs as ``RenderWorkflow``: ``render_started`` references the sealed job
  (id, final head, seal anchor key and VersionId, completeness basis, versions, options), files are
  committed in bounded ``render_files_batch`` events, ``render_completed`` carries the totals and the
  root over the batch roots; the stream verifies and is sealed; the job's chain is untouched;
- a crash at every boundary resumes to the same sealed render, and the stored bytes equal an
  independent in-memory rendering (the render id reproduces the same bytes); a real SIGKILL of the
  worker process mid-render too;
- refusals (job not sealed, matter or client closed, a chain that fails verification) are recorded
  as a sealed ``render_refused`` stream with nothing stored; an integrity failure ends ``failed``,
  sealed, with an alert;
- render anchors carry the render id and are extended with the job's matter.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Replayer, Worker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now
from edisc_custody.log import verify_chain
from edisc_custody.render_files import batches_root
from edisc_custody.retention_extension import extend_retention
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_renderers.rsmf import runtime_versions
from edisc_worker.contracts import RenderRef, render_workflow_id
from edisc_worker.pipeline import CrashHooks
from edisc_worker.renders import RenderActivities, RenderRun
from edisc_worker.workflows import RenderWorkflow

from ..custody.conftest import superuser
from ..custody.test_retention_extension import window_settings
from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..pipeline.conftest import CrashAt, SimulatedCrash
from ..worker.conftest import GOLDEN, RECORD, RECORD_SUFFIX
from .conftest import drive, expected_files, new_render, render_state
from .test_render_store import _job

ROOT = Path(__file__).resolve().parents[3]
BATCH = 2  # files per render_files_batch: several batches even for a small job


def small_batches(settings: Settings, **extra: Any) -> Settings:
    return settings.model_copy(update={"render_files_batch_size": BATCH, **extra})


def ref(t: Tenant, render_id: uuid.UUID, **kw: Any) -> RenderRef:
    fast: dict[str, Any] = {
        "heartbeat_timeout_seconds": 10,
        "retry_initial_seconds": 0.1,
        "retry_max_seconds": 0.5,
        "max_attempts": 4,
    }
    return RenderRef(str(t.tenant_id), str(render_id), **{**fast, **kw})


async def _head(sessions: Sessions, t: Tenant, stream: uuid.UUID) -> tuple[int, str]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        row = (
            await s.execute(
                text("SELECT last_seq, last_hash FROM custody_chain_heads WHERE stream_id = :s"),
                {"s": stream},
            )
        ).one()
    return row.last_seq, row.last_hash


async def _replay_and_record(client: Client, workflow_id: str, name: str) -> None:
    histories = []
    for _ in range(50):  # visibility is eventually consistent
        histories = [
            await client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history()
            async for wf in client.list_workflows(f"WorkflowId = '{workflow_id}'")
        ]
        if histories:
            break
        await asyncio.sleep(0.2)
    assert histories
    for h in histories:
        await Replayer(workflows=[RenderWorkflow]).replay_workflow(h)
    if RECORD:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        doc = {
            "workflow_id": histories[0].workflow_id,
            "history": json.loads(histories[0].to_json()),
        }
        (GOLDEN / f"{name}{RECORD_SUFFIX}-0.json").write_text(
            json.dumps(doc, indent=1, sort_keys=True)
        )


async def _run_workflow(
    temporal: Client, acts: RenderActivities, t: Tenant, render_id: uuid.UUID
) -> dict[str, Any]:
    queue = f"renders-test-{uuid.uuid4().hex[:12]}"
    async with Worker(
        temporal, task_queue=queue, workflows=[RenderWorkflow], activities=acts.all()
    ):
        handle = await temporal.start_workflow(
            RenderWorkflow.run, ref(t, render_id), id=render_workflow_id(str(render_id)),
            task_queue=queue,
        )  # fmt: skip
        try:
            async with asyncio.timeout(90):  # fail_render retries forever: fail, never hang
                result: dict[str, Any] = await handle.result()
        except TimeoutError:
            await handle.terminate("test timed out")
            raise
    return result


# ------------------------------------------------------------------ the clean path, through Temporal
async def test_a_render_workflow_records_a_sealed_stream_referencing_the_sealed_job(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client
) -> None:
    rs = small_batches(settings)
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    job_head = await _head(app_sessions, t, job_id)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)

    result = await _run_workflow(temporal, RenderActivities(app_sessions, s3, rs), t, render_id)
    assert result["status"] == "completed"
    st = await render_state(app_sessions, t.tenant_id, render_id)
    row = st["row"]

    # the files: exactly an independent rendering of the job, each stored once as a production
    want = await expected_files(app_sessions, s3, rs, t.tenant_id, job_id)
    assert st["files"] == want and len(want) > 2 * BATCH
    assert st["productions"] == {"complete": len(want)}
    assert row.file_count == row.files_done == len(want)

    # the stream: started, bounded batches, completed; render id set and job id NULL on every event
    n_batches = -(-len(want) // BATCH)
    assert st["types"] == [
        "render_started",
        *["render_files_batch"] * n_batches,
        "render_completed",
    ]
    assert all(e.job_id is None and e.render_id == render_id for e in st["events"])
    started = st["events"][0].payload
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        job = (
            await s.execute(
                text("SELECT status, seal_storage_key FROM collection_jobs WHERE id = :j"),
                {"j": job_id},
            )
        ).one()
        seal_version = (
            await s.execute(
                text("SELECT version_id FROM evidence_objects WHERE storage_key = :k"),
                {"k": job.seal_storage_key},
            )
        ).scalar_one()
    assert started["job"] == {
        "id": str(job_id),
        "status": job.status,
        "completeness_basis": "source",
        "head": {"seq": job_head[0], "hash": job_head[1]},
        "seal": {"key": job.seal_storage_key, "version_id": seal_version},
    }
    assert {k: started[k] for k in runtime_versions()} == runtime_versions()
    assert started["options"] == {"include_context": True, "time_zone": "UTC", "cap": 10_000}
    batches = [e.payload for e in st["events"][1:-1]]
    assert [b["batch"] for b in batches] == list(range(n_batches))
    assert [b["first_ord"] for b in batches] == list(range(0, len(want), BATCH))
    done = st["events"][-1].payload
    assert (done["file_count"], done["batch_count"]) == (len(want), n_batches)
    assert done["batches_root"] == batches_root([b["merkle_root"] for b in batches])
    assert done["reconciliation"]["items_in"] == done["reconciliation"]["events_out"] > 0
    assert done["reconciliation"]["renderer_version"] == runtime_versions()["renderer_version"]

    # verified and sealed; the job's own chain did not move
    report = await verify_chain(
        app_sessions, s3, rs, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert report.ok, report.errors
    assert report.files_checked == len(want) and report.batches_checked == n_batches
    assert row.sealed_at is not None and (row.head_seq, row.head_hash) == await _head(
        app_sessions, t, render_id
    )
    assert await _head(app_sessions, t, job_id) == job_head
    assert st["audits"] == ["audit.render_completed"]

    # anchors of the render stream carry the render id (no job id), under the matter's retention
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        anchors = (
            await s.execute(
                text(
                    "SELECT job_id, retain_until, storage_key FROM evidence_objects"
                    " WHERE render_id = :r AND kind = 'anchor'"
                ),
                {"r": render_id},
            )
        ).all()
    assert anchors and all(a.job_id is None for a in anchors)
    assert row.seal_storage_key in {a.storage_key for a in anchors}
    retain = effective_retain_until(rs, t.retention)
    assert all(abs((ensure_utc(a.retain_until) - retain).total_seconds()) < 120 for a in anchors)
    await _replay_and_record(temporal, render_workflow_id(str(render_id)), "render-clean")


async def test_an_identical_request_returns_the_live_render(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    first = await asyncio.gather(
        *(new_render(app_sessions, t.tenant_id, job_id) for _ in range(20))
    )
    assert len(set(first)) == 1
    from edisc_renderers.rsmf import RenderOptions

    other = await new_render(
        app_sessions, t.tenant_id, job_id, RenderOptions(include_context=False)
    )
    assert other != first[0]


# ------------------------------------------------------------------ crashes
CRASH_POINTS = [
    ("after_begin", 1),
    ("after_batch", 1),
    ("after_batch", 2),
    ("after_files", 1),
    ("after_completed", 1),
    ("after_seal_anchor", 1),
]


@pytest.mark.parametrize(("point", "nth"), CRASH_POINTS, ids=[f"{p}-{n}" for p, n in CRASH_POINTS])
async def test_a_crash_at_every_boundary_resumes_to_the_same_sealed_render(
    app_sessions: Sessions, s3: S3Client, settings: Settings, point: str, nth: int
) -> None:
    rs = small_batches(settings)
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    with pytest.raises(SimulatedCrash):
        await drive(RenderRun(app_sessions, s3, rs, CrashAt(point, nth)), t.tenant_id, render_id)
    # a fresh process resumes from the database alone, from the start of the workflow
    result = await drive(RenderRun(app_sessions, s3, rs, CrashHooks()), t.tenant_id, render_id)
    assert result["status"] == "completed"
    st = await render_state(app_sessions, t.tenant_id, render_id)
    want = await expected_files(app_sessions, s3, rs, t.tenant_id, job_id)
    assert st["files"] == want  # the same bytes as an independent rendering
    assert st["productions"] == {"complete": len(want)}  # each written once, none left pending
    n_batches = -(-len(want) // BATCH)
    assert Counter(st["types"]) == {
        "render_started": 1, "render_files_batch": n_batches, "render_completed": 1,
    }  # fmt: skip
    assert st["audits"] == ["audit.render_completed"]
    report = await verify_chain(
        app_sessions, s3, rs, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert report.ok, report.errors


async def test_a_batch_that_re_renders_differently_is_an_incident(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """A batch an earlier attempt committed must re-render to exactly what it recorded."""
    rs = small_batches(settings)
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    with pytest.raises(SimulatedCrash):
        await drive(
            RenderRun(app_sessions, s3, rs, CrashAt("after_batch", 1)), t.tenant_id, render_id
        )
    conn = await superuser(rs)
    try:  # the recorded record of file 0 no longer matches what the job renders to
        await conn.execute(
            "UPDATE render_files SET record = jsonb_set(record, '{event_count}', '999')"
            " WHERE render_id = $1 AND ord = 0",
            render_id,
        )
    finally:
        await conn.close()
    from edisc_worker.renders import RenderIntegrityError

    with pytest.raises(RenderIntegrityError, match="re-renders to other files"):
        await RenderRun(app_sessions, s3, rs).render_files(t.tenant_id, render_id)


async def test_sigkill_of_the_render_worker_resumes_to_the_same_files(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client, tmp_path: Path
) -> None:
    """The render worker is a separate PROCESS, SIGKILLed after its first committed batch; a new
    worker process finishes the same render with the same bytes."""
    from .test_render_store import SPEC

    spec = SPEC.model_copy(update={"conversations": 6, "days": 3})
    t = await new_tenant(app_sessions)
    job_id = await _spec_job(app_sessions, s3, settings, t, spec)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    queue = f"renders-kill-{uuid.uuid4().hex[:8]}"
    env = {**os.environ, "EDISC_RENDER_FILES_BATCH_SIZE": "1"}

    async def spawn(n: int) -> asyncio.subprocess.Process:
        log = (tmp_path / f"worker-{n}.log").open("wb")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "edisc_worker", "--source", "dummy", "--renders",
            "--queue", f"collect-unused-{uuid.uuid4().hex[:8]}", "--renders-queue", queue,
            cwd=ROOT, env=env, stdout=log, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        log.close()
        return proc

    worker = await spawn(1)
    try:
        handle = await temporal.start_workflow(
            RenderWorkflow.run, ref(t, render_id, heartbeat_timeout_seconds=3),
            id=render_workflow_id(str(render_id)), task_queue=queue,
        )  # fmt: skip
        async with asyncio.timeout(90):
            while True:
                row = (await render_state(app_sessions, t.tenant_id, render_id))["row"]
                if row.batches_done >= 1:
                    break
                await asyncio.sleep(0.02)
        worker.send_signal(signal.SIGKILL)
        await worker.wait()
        mid = (await render_state(app_sessions, t.tenant_id, render_id))["row"]
        assert mid.status == "rendering", "the render finished before the kill"
        worker = await spawn(2)
        async with asyncio.timeout(120):
            result = await handle.result()
    finally:
        if worker.returncode is None:
            worker.send_signal(signal.SIGKILL)
            await worker.wait()
    assert result["status"] == "completed"
    st = await render_state(app_sessions, t.tenant_id, render_id)
    assert st["files"] == await expected_files(app_sessions, s3, settings, t.tenant_id, job_id)
    assert st["row"].batches_done == st["row"].file_count  # batch size 1 in the worker processes
    report = await verify_chain(
        app_sessions, s3, settings, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert report.ok, report.errors


async def _spec_job(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, spec: Any
) -> uuid.UUID:
    from datetime import UTC, datetime

    from edisc_connector_dummy.connector import DummyConnector, scope_for_days
    from edisc_connector_dummy.dataset import Dataset
    from edisc_connectors_base.types import Connection
    from edisc_core.ids import new_id
    from edisc_worker.pipeline import Pipeline

    from ...unit.dummy.conftest import RecordingLimiter

    ds = Dataset(spec)
    conn = Connection(
        t.tenant_id, t.connection_id, "dummy", spec.workspace_id,
        {"spec": spec.model_dump(mode="json"), "epoch": 0},
    )  # fmt: skip
    start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
    job_id = new_id()
    p = Pipeline(sessions, s3, settings, DummyConnector(RecordingLimiter()), CrashHooks())
    await p.start_job(
        tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id, connection_id=t.connection_id,
        scopes=[scope_for_days("*", start, ds.n_days(0))], requested_by="tester",
    )  # fmt: skip
    await p.run(tenant_id=t.tenant_id, job_id=job_id, conn=conn)
    return job_id


# ------------------------------------------------------------------ refusals and failures
async def _assert_refused(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, render_id: uuid.UUID,
    reason: str,
) -> None:  # fmt: skip
    st = await render_state(sessions, t.tenant_id, render_id)
    assert (st["row"].status, st["row"].reason) == ("refused", reason)
    assert st["types"] == ["render_refused"]
    assert st["events"][0].payload["reason"] == reason
    assert st["productions"] == {} and st["files"] == []  # nothing rendered or stored
    assert st["row"].sealed_at is not None and st["audits"] == ["audit.render_refused"]
    report = await verify_chain(
        sessions, s3, settings, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert report.ok, report.errors


async def test_a_job_chain_that_fails_verification_is_refused(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client
) -> None:
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    conn = await superuser(settings)
    try:  # a DBA rewrites one event of the sealed job's chain
        await conn.execute(
            "UPDATE custody_events SET payload = jsonb_set(payload, '{unit_key}', '\"forged\"')"
            " WHERE stream_id = $1 AND seq = 3",
            job_id,
        )
    finally:
        await conn.close()
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    result = await _run_workflow(
        temporal, RenderActivities(app_sessions, s3, settings), t, render_id
    )
    assert result["status"] == "refused"
    await _assert_refused(app_sessions, s3, settings, t, render_id, "chain_verification_failed")


@pytest.mark.parametrize("closed", ["matter", "client", "unsealed"])
async def test_a_closed_matter_or_an_unsealed_job_is_refused_by_the_workflow(
    app_sessions: Sessions, s3: S3Client, settings: Settings, closed: str
) -> None:
    """The API refuses these before creating anything; the workflow re-checks (state can change
    between the request and the render)."""
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    conn = await superuser(settings)
    try:
        if closed == "matter":
            await conn.execute(
                "UPDATE matters SET closed_at = now(), closed_by = 'tests' WHERE id = $1",
                t.matter_id,
            )
        elif closed == "client":
            await conn.execute(
                "UPDATE clients SET closed_at = now(), closed_by = 'tests'"
                " WHERE id = (SELECT client_id FROM matters WHERE id = $1)",
                t.matter_id,
            )
        else:
            await conn.execute("UPDATE collection_jobs SET sealed_at = NULL WHERE id = $1", job_id)
    finally:
        await conn.close()
    result = await drive(RenderRun(app_sessions, s3, settings), t.tenant_id, render_id)
    assert result["status"] == "refused"
    reason = {"matter": "matter_closed", "client": "client_closed", "unsealed": "job_not_sealed"}
    await _assert_refused(app_sessions, s3, settings, t, render_id, reason[closed])


async def test_an_integrity_failure_fails_and_seals_the_render(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client
) -> None:
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    conn = await superuser(settings)
    try:  # a page's registry hash no longer matches its stored bytes
        await conn.execute(
            "UPDATE evidence_objects SET sha256 = repeat('0', 64), source_sha256 = repeat('0', 64)"
            " WHERE id = (SELECT i.evidence_object_id FROM job_items ji JOIN items i ON i.id = ji.item_id"
            " WHERE ji.job_id = $1 AND ji.in_scope AND i.item_type = 'message' ORDER BY i.id LIMIT 1)",
            job_id,
        )
    finally:
        await conn.close()
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    result = await _run_workflow(
        temporal, RenderActivities(app_sessions, s3, settings), t, render_id
    )
    assert result["status"] == "failed"
    st = await render_state(app_sessions, t.tenant_id, render_id)
    assert st["types"] == ["render_started", "render_failed"]
    failed = st["events"][-1].payload
    assert (
        failed["error_type"] == "RenderInputIntegrityError" and failed["from_status"] == "rendering"
    )
    assert st["productions"] == {} and st["row"].sealed_at is not None
    assert st["audits"] == ["audit.render_failed"]
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        alerts = (
            (
                await s.execute(
                    text("SELECT message FROM alerts WHERE kind = 'render_failed' AND job_id = :j"),
                    {"j": job_id},
                )
            )
            .scalars()
            .all()
        )
    assert len(alerts) == 1 and str(render_id) in alerts[0]
    report = await verify_chain(
        app_sessions, s3, settings, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert report.ok, report.errors
    # a failed render does not block a new render of the same job and options
    assert await new_render(app_sessions, t.tenant_id, job_id) != render_id
    await _replay_and_record(temporal, render_workflow_id(str(render_id)), "render-failed")


# ------------------------------------------------------------------ retention
async def test_render_anchors_are_extended_with_the_matter(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    rs = window_settings(settings)
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    await drive(RenderRun(app_sessions, s3, settings), t.tenant_id, render_id)

    async def anchors() -> dict[uuid.UUID, Any]:
        async with tenant_tx(app_sessions, t.tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id, retain_until FROM evidence_objects WHERE render_id = :r"
                        " AND kind = 'anchor' AND job_id IS NULL"
                    ),
                    {"r": render_id},
                )
            ).all()
        return {r.id: ensure_utc(r.retain_until) for r in rows}

    before = await anchors()
    assert before
    now = utc_now()
    await extend_retention(sweeper_sessions, app_sessions, s3, rs, now=now, tenant_id=t.tenant_id)
    after = await anchors()
    target = effective_retain_until(rs, t.retention, now=now)
    assert set(after) == set(before)
    for anchor_id, until in after.items():
        assert until > before[anchor_id]
        assert abs((until - target).total_seconds()) < 5, (until, target)
        assert until <= ensure_utc(t.retention)  # never past the matter's retention date
