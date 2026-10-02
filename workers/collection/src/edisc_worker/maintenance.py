"""Periodic sweepers as Temporal Schedules (ADR 0012 section 7).

Each schedule starts ``MaintenanceWorkflow`` (one activity) on the ``maintenance`` task queue with
overlap SKIP: a sweep still running when the next one is due is never doubled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.recovery import sweep_stale_uploads
from edisc_custody.retention_extension import extend_retention
from edisc_custody.sweeper import sweep_anchors
from edisc_db.connection_tokens import reconcile_token_refreshes
from edisc_worker.contracts import MAINTENANCE_QUEUE
from edisc_worker.workflows import MaintenanceWorkflow


@dataclass(frozen=True)
class Sweep:
    schedule_id: str
    activity: str
    every: timedelta


SWEEPS = (
    Sweep("sweep-anchors", "sweep_anchors", timedelta(minutes=5)),
    Sweep("reconcile-token-refreshes", "reconcile_token_refreshes", timedelta(minutes=10)),
    Sweep("sweep-stale-uploads", "sweep_stale_uploads", timedelta(hours=1)),
    Sweep("extend-retention", "extend_retention", timedelta(hours=6)),
)


@dataclass
class MaintenanceActivities:
    sweeper_sessions: async_sessionmaker[AsyncSession]
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    settings: Settings
    tenant_id: uuid.UUID | None = (
        None  # None (production): every tenant; set: one tenant (tests, ops)
    )

    @activity.defn(name="sweep_anchors")
    async def sweep_anchors(self) -> dict[str, Any]:
        result = await sweep_anchors(
            self.sweeper_sessions, self.sessions, self.s3, self.settings, tenant_id=self.tenant_id
        )
        return {"anchored": len(result.anchored), "streams_seen": result.streams_seen}

    @activity.defn(name="reconcile_token_refreshes")
    async def reconcile_token_refreshes(self) -> dict[str, Any]:
        return dict(await reconcile_token_refreshes(self.sweeper_sessions, self.sessions))

    @activity.defn(name="sweep_stale_uploads")
    async def sweep_stale_uploads(self) -> dict[str, Any]:
        result = await sweep_stale_uploads(
            self.sweeper_sessions, self.sessions, self.s3, self.settings, tenant_id=self.tenant_id
        )
        return {"jobs": result.jobs}

    @activity.defn(name="extend_retention")
    async def extend_retention(self) -> dict[str, Any]:
        result = await extend_retention(
            self.sweeper_sessions, self.sessions, self.s3, self.settings, tenant_id=self.tenant_id
        )
        return {
            "tenants": result.tenants,
            "examined": dict(result.examined),
            "extended": dict(result.extended),
        }

    def all(self) -> list[Any]:
        return [
            self.sweep_anchors,
            self.reconcile_token_refreshes,
            self.sweep_stale_uploads,
            self.extend_retention,
        ]


def schedule_for(sweep: Sweep, queue: str) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            MaintenanceWorkflow.run,
            sweep.activity,
            id=f"maintenance-{sweep.schedule_id}",
            task_queue=queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=sweep.every)]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def ensure_schedules(
    client: Client, *, queue: str = MAINTENANCE_QUEUE, prefix: str = ""
) -> list[str]:
    """Create or update every sweeper schedule (idempotent; run at every maintenance-worker start)."""
    ids: list[str] = []
    for sweep in SWEEPS:
        schedule_id = f"{prefix}{sweep.schedule_id}"
        schedule = schedule_for(sweep, queue)
        try:
            await client.create_schedule(schedule_id, schedule)
        except ScheduleAlreadyRunningError:

            def replace(_: ScheduleUpdateInput, new: Schedule = schedule) -> ScheduleUpdate:
                return ScheduleUpdate(schedule=new)

            await client.get_schedule_handle(schedule_id).update(replace)
        ids.append(schedule_id)
    return ids
