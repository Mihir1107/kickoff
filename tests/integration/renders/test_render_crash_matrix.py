"""The full render crash matrix (M15 step 5). A crash (simulated like SIGKILL: nothing after the point
runs; an open transaction rolls back) at every boundary of a render, at its first and, where it
repeats, its second occurrence; then a fresh process resumes from the database alone. Every point
ends with:

- the stored bytes equal to an independent in-memory rendering (the oracle);
- every custody event and audit event exactly once; the render sealed and its chain verified;
- no pending production row, and exactly one object version at every production and anchor key;
- an exported render package that ``edisc-verify`` accepts.

The failure path and the refused path are crashed at their own boundaries. Also: a real SIGKILL of
the worker process during planning, the anchor sweeper racing a recovering render on the same tail,
and two identical render requests racing on the deduplication key.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import verify_chain
from edisc_custody.render_export import export_render_package
from edisc_custody.render_package import verify_render_package
from edisc_custody.sweeper import sweep_anchors
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_renderers.rsmf import RenderOptions, runtime_versions
from edisc_worker.contracts import RenderRef, render_task_queue, render_workflow_id
from edisc_worker.renders import RenderActivities, RenderRun, create_render, start_render_workflow
from edisc_worker.workflows import RenderWorkflow

from ..custody.conftest import superuser
from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..pipeline.conftest import CrashAt, SimulatedCrash
from .conftest import drive, expected_files, new_render, render_state
from .test_render_store import _job

ROOT = Path(__file__).resolve().parents[3]
BATCH = 2

SINGLE = [
    "begin_checked", "begin_tx", "begin_committed", "after_begin", "planned", "files_tx",
    "after_files", "complete_tx", "complete_committed", "after_completed",
]  # fmt: skip
# the seal's sub-steps on the clean path are covered by real SIGKILLs at a test-only barrier
# (test_sigkill_at_every_seal_sub_step); the failure and refused paths crash them simulated
SEAL_STEPS = ["seal_start", "after_seal_anchor", "seal_tx", "sealed"]
REPEATED = ["planning", "mid_upload", "file_stored", "batch_tx", "batch_committed", "after_batch"]
POINTS = [(p, 1) for p in SINGLE] + [(p, n) for p in REPEATED for n in (1, 2)]


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"render_files_batch_size": BATCH})


async def drive_like_the_workflow(
    run: RenderRun, t: Tenant, render_id: uuid.UUID
) -> dict[str, Any]:
    """begin, files, complete; an ordinary failure goes to fail (as RenderWorkflow does). A
    SimulatedCrash (a BaseException) is a kill: it propagates."""
    try:
        return await drive(run, t.tenant_id, render_id)
    except Exception as exc:
        return await run.fail(t.tenant_id, render_id, type(exc).__name__, str(exc))


async def _versions(s3: S3Client, settings: Settings, prefix: str) -> Counter[str]:
    resp = await s3.list_object_versions(Bucket=settings.s3_evidence_bucket, Prefix=prefix)
    assert not resp.get("DeleteMarkers")
    return Counter(v["Key"] for v in resp.get("Versions", []))


async def assert_one_version_per_key(
    s3: S3Client, settings: Settings, t: Tenant, render_id: uuid.UUID
) -> None:
    for prefix in (
        f"t/{t.tenant_id}/productions/{render_id}/",
        f"custody-anchors/{t.tenant_id}/{render_id}/",
    ):
        counts = await _versions(s3, settings, prefix)
        assert all(n == 1 for n in counts.values()), {k: n for k, n in counts.items() if n != 1}


async def assert_final(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, render_id: uuid.UUID,
    status: str, types: Counter[str], tmp: Path,
) -> dict[str, Any]:  # fmt: skip
    st = await render_state(sessions, t.tenant_id, render_id)
    assert st["row"].status == status and st["row"].sealed_at is not None
    assert Counter(st["types"]) == types, st["types"]
    assert st["audits"] == [f"audit.render_{status}"]
    assert "pending" not in st["productions"]
    await assert_one_version_per_key(s3, settings, t, render_id)
    report = await verify_chain(
        sessions, s3, settings, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert report.ok, report.errors
    pkg = await export_render_package(
        sessions, s3, settings, tenant_id=t.tenant_id, render_id=render_id, dest=tmp / "pkg"
    )
    verified = verify_render_package(pkg)
    assert verified.ok, (verified.errors, verified.chain.errors if verified.chain else None)
    return st


# ------------------------------------------------------------------ the clean path
@pytest.mark.parametrize(("point", "nth"), POINTS, ids=[f"{p}-{n}" for p, n in POINTS])
async def test_a_crash_at_every_boundary_resumes_exactly(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path, point: str, nth: int
) -> None:
    rs = _settings(settings)
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    with pytest.raises(SimulatedCrash):
        await drive_like_the_workflow(
            RenderRun(app_sessions, s3, rs, CrashAt(point, nth)), t, render_id
        )
    result = await drive_like_the_workflow(RenderRun(app_sessions, s3, rs), t, render_id)
    assert result["status"] == "completed", result
    want = await expected_files(app_sessions, s3, rs, t.tenant_id, job_id)
    st = await assert_final(
        app_sessions, s3, rs, t, render_id, "completed",
        Counter({"render_started": 1, "render_files_batch": -(-len(want) // BATCH),
                 "render_completed": 1}),
        tmp_path,
    )  # fmt: skip
    assert st["files"] == want
    assert st["productions"] == {"complete": len(want)}


async def test_a_crash_after_an_object_is_written_but_before_its_row_completes(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """The object version exists, the registry row is still pending: the resumed render pins that
    stored version (no second version at the key)."""
    rs = _settings(settings)
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    original = EvidenceWriter._complete
    fired: list[int] = []

    async def crash_once(self: EvidenceWriter, *args: Any, **kwargs: Any) -> Any:
        if not fired:
            fired.append(1)
            raise SimulatedCrash("after the object was written")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(EvidenceWriter, "_complete", crash_once)
    with pytest.raises(SimulatedCrash):
        await drive_like_the_workflow(RenderRun(app_sessions, s3, rs), t, render_id)
    monkeypatch.setattr(EvidenceWriter, "_complete", original)
    st = await render_state(app_sessions, t.tenant_id, render_id)
    assert st["productions"].get("pending") == 1  # the row the crash left
    result = await drive_like_the_workflow(RenderRun(app_sessions, s3, rs), t, render_id)
    assert result["status"] == "completed"
    want = await expected_files(app_sessions, s3, rs, t.tenant_id, job_id)
    st = await assert_final(
        app_sessions, s3, rs, t, render_id, "completed",
        Counter({"render_started": 1, "render_files_batch": -(-len(want) // BATCH),
                 "render_completed": 1}),
        tmp_path,
    )  # fmt: skip
    assert st["files"] == want


# ------------------------------------------------------------------ the failure and refused paths
FAIL_POINTS = ["fail_tx", "fail_committed", "seal_tx", "sealed"]


@pytest.mark.parametrize("point", FAIL_POINTS)
async def test_a_crash_on_the_failure_path_still_ends_failed_and_sealed(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path, point: str
) -> None:
    rs = _settings(settings)
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    conn = await superuser(rs)
    try:  # a page whose registry hash no longer matches: the render fails during planning
        await conn.execute(
            "UPDATE evidence_objects SET sha256 = repeat('0', 64), source_sha256 = repeat('0', 64)"
            " WHERE id = (SELECT i.evidence_object_id FROM job_items ji JOIN items i ON i.id = ji.item_id"
            " WHERE ji.job_id = $1 AND ji.in_scope AND i.item_type = 'message' ORDER BY i.id LIMIT 1)",
            job_id,
        )
    finally:
        await conn.close()
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    with pytest.raises(SimulatedCrash):
        await drive_like_the_workflow(
            RenderRun(app_sessions, s3, rs, CrashAt(point, 1)), t, render_id
        )
    result = await drive_like_the_workflow(RenderRun(app_sessions, s3, rs), t, render_id)
    assert result["status"] == "failed"
    st = await assert_final(
        app_sessions, s3, rs, t, render_id, "failed",
        Counter({"render_started": 1, "render_failed": 1}), tmp_path,
    )  # fmt: skip
    assert st["productions"] == {}
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        alerts = (
            await s.execute(
                text("SELECT count(*) FROM alerts WHERE kind = 'render_failed' AND job_id = :j"),
                {"j": job_id},
            )
        ).scalar_one()
    assert alerts == 1


REFUSED_POINTS = ["begin_tx", "begin_committed", "seal_tx", "sealed"]


@pytest.mark.parametrize("point", REFUSED_POINTS)
async def test_a_crash_on_the_refused_path_still_ends_refused_and_sealed(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path, point: str
) -> None:
    rs = _settings(settings)
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    conn = await superuser(rs)
    try:
        await conn.execute(
            "UPDATE custody_events SET actor = 'mallory' WHERE stream_id = $1 AND seq = 2", job_id
        )
    finally:
        await conn.close()
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    with pytest.raises(SimulatedCrash):
        await drive_like_the_workflow(
            RenderRun(app_sessions, s3, rs, CrashAt(point, 1)), t, render_id
        )
    result = await drive_like_the_workflow(RenderRun(app_sessions, s3, rs), t, render_id)
    assert result["status"] == "refused"
    st = await assert_final(
        app_sessions, s3, rs, t, render_id, "refused", Counter({"render_refused": 1}), tmp_path
    )
    assert st["productions"] == {} and st["row"].reason == "chain_verification_failed"


# ------------------------------------------------------------------ races
async def test_the_sweeper_and_a_recovering_render_anchor_the_same_tail_once(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings,
    tmp_path: Path,
) -> None:  # fmt: skip
    """A render dies leaving batches that were not due for an anchor. The periodic sweeper and the
    recovering render then anchor that tail at the same time: one object per anchor key (the claim
    coalesces them), and the chain verifies."""
    rs = settings.model_copy(
        update={"render_files_batch_size": 1, "custody_anchor_every_n_batches": 1000}
    )
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    with pytest.raises(SimulatedCrash):
        await drive_like_the_workflow(
            RenderRun(app_sessions, s3, rs, CrashAt("after_batch", 3)), t, render_id
        )
    sweep, result = await asyncio.gather(
        sweep_anchors(
            sweeper_sessions, app_sessions, s3, rs, idle=timedelta(0), tenant_id=t.tenant_id
        ),
        drive_like_the_workflow(RenderRun(app_sessions, s3, rs), t, render_id),
    )
    assert result["status"] == "completed"
    want = await expected_files(app_sessions, s3, rs, t.tenant_id, job_id)
    await assert_final(
        app_sessions, s3, rs, t, render_id, "completed",
        Counter({"render_started": 1, "render_files_batch": len(want), "render_completed": 1}),
        tmp_path,
    )  # fmt: skip
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT storage_key FROM evidence_objects WHERE render_id = :r AND kind = 'anchor'"
                    ),
                    {"r": render_id},
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == len(set(rows))  # one registry row per anchor key


async def test_two_identical_requests_racing_make_one_render_and_one_set_of_events(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client, tmp_path: Path
) -> None:
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    versions = runtime_versions()

    async def request() -> uuid.UUID:  # what the API does: create (deduplicated), then start
        async with tenant_tx(app_sessions, t.tenant_id) as s:
            made = await create_render(
                s, tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
                options=RenderOptions(),
                requested_by="tests", versions=versions,
            )  # fmt: skip
        await start_render_workflow(temporal, settings, t.tenant_id, made.render_id, versions)
        return made.render_id

    acts = RenderActivities(app_sessions, s3, settings)
    async with Worker(temporal, task_queue=acts.task_queue, workflows=[RenderWorkflow],
                      activities=acts.all()):  # fmt: skip
        first, second = await asyncio.gather(request(), request())
        assert first == second
        async with asyncio.timeout(90):
            result = await temporal.get_workflow_handle(render_workflow_id(str(first))).result()
    assert result["status"] == "completed"
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        renders = (
            await s.execute(text("SELECT count(*) FROM renders WHERE job_id = :j"), {"j": job_id})
        ).scalar_one()
    assert renders == 1
    want = await expected_files(app_sessions, s3, settings, t.tenant_id, job_id)
    await assert_final(
        app_sessions, s3, settings, t, first, "completed",
        Counter({"render_started": 1, "render_files_batch": -(-len(want) // settings.render_files_batch_size),
                 "render_completed": 1}),
        tmp_path,
    )  # fmt: skip


# ------------------------------------------------------------------ a real SIGKILL during planning
async def test_sigkill_of_the_render_worker_during_planning(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client, tmp_path: Path
) -> None:
    """One slice of 10,001 events makes planning long enough to kill the worker PROCESS inside it
    (status rendering, nothing stored yet); a new worker process finishes with the oracle's bytes."""
    from ..corpus.cases import CASES
    from ..corpus.test_corpus import collect

    case = CASES["cap_10001"]
    t = await new_tenant(app_sessions)
    job_id = await collect(app_sessions, s3, settings, t, case)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    queue = f"{render_task_queue(**runtime_versions())}.kill-{uuid.uuid4().hex[:6]}"

    async def spawn(n: int) -> asyncio.subprocess.Process:
        log = (tmp_path / f"worker-{n}.log").open("wb")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "edisc_worker", "--source", "dummy", "--renders",
            "--queue", f"collect-unused-{uuid.uuid4().hex[:8]}", "--renders-queue", queue,
            cwd=ROOT, env=dict(os.environ), stdout=log, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        log.close()
        return proc

    worker = await spawn(1)
    try:
        handle = await temporal.start_workflow(
            RenderWorkflow.run,
            RenderRef(str(t.tenant_id), str(render_id), heartbeat_timeout_seconds=3,
                      retry_initial_seconds=0.1, retry_max_seconds=0.5, max_attempts=4),
            id=render_workflow_id(str(render_id)), task_queue=queue,
        )  # fmt: skip
        async with asyncio.timeout(90):
            while True:
                row = (await render_state(app_sessions, t.tenant_id, render_id))["row"]
                if row.status == "rendering":
                    break
                await asyncio.sleep(0.02)
        worker.send_signal(signal.SIGKILL)
        await worker.wait()
        mid = await render_state(app_sessions, t.tenant_id, render_id)
        assert mid["row"].status == "rendering" and mid["productions"] == {}, (
            "not killed in planning"
        )
        worker = await spawn(2)
        async with asyncio.timeout(110):
            result = await handle.result()
    finally:
        if worker.returncode is None:
            worker.send_signal(signal.SIGKILL)
            await worker.wait()
    assert result["status"] == "completed"
    want = await expected_files(app_sessions, s3, settings, t.tenant_id, job_id)
    st = await assert_final(
        app_sessions, s3, settings, t, render_id, "completed",
        Counter({"render_started": 1, "render_files_batch": 1, "render_completed": 1}), tmp_path,
    )  # fmt: skip
    assert st["files"] == want and len(want) == 2


# ------------------------------------------------------------------ real SIGKILLs inside the seal
async def _spawn(tmp: Path, n: int, queue: str, env: dict[str, str]) -> asyncio.subprocess.Process:
    log = (tmp / f"worker-{n}.log").open("wb")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "edisc_worker", "--source", "dummy", "--renders",
        "--queue", f"collect-unused-{uuid.uuid4().hex[:8]}", "--renders-queue", queue,
        cwd=ROOT, env=env, stdout=log, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    log.close()
    return proc


@pytest.mark.parametrize("step", SEAL_STEPS)
async def test_sigkill_at_every_seal_sub_step(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client, tmp_path: Path,
    step: str,
) -> None:  # fmt: skip
    """The worker PROCESS blocks at a test-only barrier at one seal sub-step (before the forced
    anchor, after it, inside the transaction that records the seal, after that commit), is SIGKILLed
    while it waits, and a new worker process finishes: sealed once, audited once, the oracle's bytes."""
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    queue = f"{render_task_queue(**runtime_versions())}.seal-{uuid.uuid4().hex[:6]}"
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    worker = await _spawn(
        tmp_path, 1, queue, {**os.environ, "EDISC_TEST_RENDER_BARRIER": f"{step}:{barrier}"}
    )
    try:
        handle = await temporal.start_workflow(
            RenderWorkflow.run,
            RenderRef(str(t.tenant_id), str(render_id), heartbeat_timeout_seconds=3,
                      retry_initial_seconds=0.1, retry_max_seconds=0.5, max_attempts=4),
            id=render_workflow_id(str(render_id)), task_queue=queue,
        )  # fmt: skip
        reached = barrier / f"{step}.reached"
        async with asyncio.timeout(90):
            while not reached.exists():  # noqa: ASYNC110 - a file written by another process
                await asyncio.sleep(0.02)
        worker.send_signal(signal.SIGKILL)
        await worker.wait()
        mid = (await render_state(app_sessions, t.tenant_id, render_id))["row"]
        assert mid.status == "completed"
        assert (mid.seal_storage_key is not None) is (step == "sealed"), (
            step
        )  # where the kill landed
        worker = await _spawn(tmp_path, 2, queue, dict(os.environ))
        async with asyncio.timeout(90):
            result = await handle.result()
    finally:
        if worker.returncode is None:
            worker.send_signal(signal.SIGKILL)
            await worker.wait()
    assert result["status"] == "completed"
    want = await expected_files(app_sessions, s3, settings, t.tenant_id, job_id)
    st = await assert_final(
        app_sessions, s3, settings, t, render_id, "completed",
        Counter({"render_started": 1,
                 "render_files_batch": -(-len(want) // settings.render_files_batch_size),
                 "render_completed": 1}),
        tmp_path,
    )  # fmt: skip
    assert st["files"] == want
