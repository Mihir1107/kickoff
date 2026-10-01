"""Sweeper schedules (ADR 0012 section 7): created idempotently, overlap SKIP, and a run works."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, ScheduleActionStartWorkflow, ScheduleOverlapPolicy
from temporalio.worker import Worker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_worker.maintenance import SWEEPS, MaintenanceActivities, ensure_schedules
from edisc_worker.workflows import MaintenanceWorkflow

Sessions = async_sessionmaker[AsyncSession]


async def test_schedules_are_idempotent_skip_overlaps_and_run(
    temporal: Client,
    app_sessions: Sessions,
    sweeper_sessions: Sessions,
    s3: S3Client,
    settings: Settings,
) -> None:
    prefix = f"test-{uuid.uuid4().hex[:10]}-"
    queue = f"maintenance-test-{uuid.uuid4().hex[:10]}"
    ids = await ensure_schedules(temporal, queue=queue, prefix=prefix)
    try:
        assert await ensure_schedules(temporal, queue=queue, prefix=prefix) == ids  # re-run: update
        for sweep, schedule_id in zip(SWEEPS, ids, strict=True):
            desc = await temporal.get_schedule_handle(schedule_id).describe()
            assert desc.schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
            assert [i.every for i in desc.schedule.spec.intervals] == [sweep.every]
            action = desc.schedule.action
            assert isinstance(action, ScheduleActionStartWorkflow)
            assert action.workflow == "MaintenanceWorkflow" and action.task_queue == queue
            assert [json.loads(a.data) for a in action.args] == [sweep.activity]  # type: ignore[union-attr]
        # scoped to an empty tenant: the shared test DB holds deliberately tampered chains
        acts = MaintenanceActivities(
            sweeper_sessions, app_sessions, s3, settings, tenant_id=uuid.uuid4()
        )
        async with Worker(
            temporal, task_queue=queue, workflows=[MaintenanceWorkflow], activities=acts.all()
        ):
            for schedule_id in ids:
                handle = temporal.get_schedule_handle(schedule_id)
                await handle.trigger()
                started = None
                for _ in range(100):
                    recent = (await handle.describe()).info.recent_actions
                    if recent:
                        started = recent[-1].action
                        break
                    await asyncio.sleep(0.1)
                assert started is not None, f"{schedule_id} never started a run"
                run = temporal.get_workflow_handle(
                    started.workflow_id, run_id=started.first_execution_run_id
                )
                result = await asyncio.wait_for(run.result(), timeout=60)
                assert isinstance(result, dict)
    finally:
        for schedule_id in ids:
            await temporal.get_schedule_handle(schedule_id).delete()


def test_every_sweep_has_a_sane_interval() -> None:
    assert {s.activity for s in SWEEPS} == {
        "sweep_anchors",
        "reconcile_token_refreshes",
        "sweep_stale_uploads",
    }
    assert all(timedelta(minutes=1) <= s.every <= timedelta(hours=1) for s in SWEEPS)
