"""ReportWorkflow on the real Temporal server (ADR 0018 §9, §13): a report end to end through the
workflow, real SIGKILLs of the report worker PROCESS at test-only barriers (the snapshot, the start,
mid-upload, a file's record, ``report_generated``, every seal sub-step), and ``ensure-job-reports``
with its ``report_missing`` episodes."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.contracts import ReportRef, report_task_queue, report_workflow_id
from edisc_worker.report_ops import ensure_job_reports
from edisc_worker.reports import ReportActivities, runtime_identity
from edisc_worker.workflows import ReportWorkflow

from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..pipeline.conftest import run_job, spec
from .conftest import assert_completed, new_report, report_state

ROOT = Path(__file__).resolve().parents[3]
FAST = {"heartbeat_timeout_seconds": 3, "retry_initial_seconds": 0.1, "retry_max_seconds": 0.5}
KILL_POINTS = [
    "snapshot_tx", "begin_tx", "mid_upload", "file_tx", "generated_tx",
    "seal_start", "after_seal_anchor", "seal_tx", "sealed",
]  # fmt: skip


async def _sealed_job(
    sessions: Sessions, s3: S3Client, settings: Settings
) -> tuple[Tenant, uuid.UUID]:
    t = await new_tenant(sessions)
    return t, (await run_job(sessions, s3, settings, t, spec(), 0)).job_id


def _queue() -> str:
    return f"{report_task_queue(**runtime_identity())}.t-{uuid.uuid4().hex[:6]}"


async def test_a_report_through_the_workflow(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    queue = _queue()
    acts = ReportActivities(app_sessions, s3, settings)
    async with Worker(
        temporal, task_queue=queue, workflows=[ReportWorkflow], activities=acts.all()
    ):
        result = await temporal.execute_workflow(
            ReportWorkflow.run, ReportRef(str(t.tenant_id), str(report_id), **FAST),
            id=report_workflow_id(str(report_id)), task_queue=queue,
        )  # fmt: skip
    assert result["status"] == "completed" and result["clean"] is True
    await assert_completed(app_sessions, s3, settings, t, job_id, report_id)


async def _spawn(tmp: Path, n: int, queue: str, env: dict[str, str]) -> asyncio.subprocess.Process:
    log = (tmp / f"worker-{n}.log").open("wb")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "edisc_worker", "--source", "dummy", "--reports",
        "--queue", f"collect-unused-{uuid.uuid4().hex[:8]}", "--reports-queue", queue,
        cwd=ROOT, env=env, stdout=log, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    log.close()
    return proc


@pytest.mark.parametrize("point", KILL_POINTS)
async def test_sigkill_of_the_report_worker_resumes_exactly(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client, tmp_path: Path,
    point: str,
) -> None:  # fmt: skip
    """The worker PROCESS blocks at a test-only barrier, is SIGKILLed while it waits, and a new
    worker process finishes the report: every event and audit once, the stored bytes equal to an
    independent build from the same snapshot, one object version per key, the chain sealed."""
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    queue = _queue()
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    worker = await _spawn(
        tmp_path, 1, queue, {**os.environ, "EDISC_TEST_REPORT_BARRIER": f"{point}:{barrier}"}
    )
    try:
        handle = await temporal.start_workflow(
            ReportWorkflow.run, ReportRef(str(t.tenant_id), str(report_id), max_attempts=6, **FAST),
            id=report_workflow_id(str(report_id)), task_queue=queue,
        )  # fmt: skip
        reached = barrier / f"{point}.reached"
        async with asyncio.timeout(90):
            while not reached.exists():  # noqa: ASYNC110 - a file written by another process
                await asyncio.sleep(0.02)
        worker.send_signal(signal.SIGKILL)
        await worker.wait()
        worker = await _spawn(tmp_path, 2, queue, dict(os.environ))
        async with asyncio.timeout(90):
            result = await handle.result()
    finally:
        if worker.returncode is None:
            worker.send_signal(signal.SIGKILL)
            await worker.wait()
    assert result["status"] == "completed", result
    await assert_completed(app_sessions, s3, settings, t, job_id, report_id)


async def _alerts(sessions: Sessions, t: Tenant, job_id: uuid.UUID) -> list[str]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text("SELECT kind FROM alerts WHERE job_id = :j ORDER BY created_at"),
                    {"j": job_id},
                )
            ).scalars()
        )


async def _episodes(sessions: Sessions, t: Tenant, job_id: uuid.UUID) -> list[Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT kind, ended_at, end_reason FROM production_episodes"
                        " WHERE job_id = :j ORDER BY started_at"
                    ),
                    {"j": job_id},
                )
            ).all()
        )


async def test_ensure_job_reports_reports_every_sealed_job_once_and_flags_a_missing_one(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings,
    temporal: Client,
) -> None:  # fmt: skip
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    acts = ReportActivities(app_sessions, s3, settings)
    late = settings.model_copy(update={"report_missing_seconds": 0})
    # nothing serves the runtime's queue yet: the report is created and waits; the job is flagged
    first = await ensure_job_reports(
        sweeper_sessions, app_sessions, temporal, late, tenant_id=t.tenant_id
    )
    assert len(first.created) == 1 and first.missing_opened == [str(job_id)]
    again = await ensure_job_reports(
        sweeper_sessions, app_sessions, temporal, late, tenant_id=t.tenant_id
    )
    assert again.created == [] and again.missing_opened == []  # one report, one episode, one alert
    assert await _alerts(app_sessions, t, job_id) == ["report_missing"]
    report_id = uuid.UUID(first.created[0])
    assert (await report_state(app_sessions, t.tenant_id, report_id))[
        "row"
    ].requested_by == "system"
    async with Worker(
        temporal, task_queue=acts.task_queue, workflows=[ReportWorkflow], activities=acts.all()
    ):
        handle = temporal.get_workflow_handle(report_workflow_id(str(report_id)))
        async with asyncio.timeout(90):
            result = await handle.result()
    assert result["status"] == "completed"
    (episode,) = await _episodes(app_sessions, t, job_id)
    assert (episode.kind, episode.end_reason) == ("report_missing", "report_completed")
    assert episode.ended_at is not None
    after = await ensure_job_reports(
        sweeper_sessions, app_sessions, temporal, late, tenant_id=t.tenant_id
    )
    assert after.created == [] and after.missing_opened == []
    assert await _alerts(app_sessions, t, job_id) == ["report_missing"]
