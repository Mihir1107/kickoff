"""Temporal activities: thin wrappers over ``Pipeline`` (ADR 0012).

Every activity:
- takes ids only and loads everything else (connection, config) from the DB, so no token, raw data or
  large payload ever enters Temporal history;
- is idempotent and resumes from the DB checkpoint (heartbeat details are informational only);
- classifies every exception (``edisc_worker.errors``) into an ``ApplicationError`` so workflows branch
  on the error class, never on raw exceptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.guard import ensure_dummy_permitted
from edisc_connectors_base.protocol import Connector
from edisc_connectors_base.types import Connection
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.contracts import (
    ChildrenRequest,
    CollectRequest,
    ErrorClass,
    JobOverview,
    JobRef,
    OverviewRequest,
    PauseRequest,
    StopRequest,
    UnitFailure,
    UnitRef,
    unit_workflow_id,
)
from edisc_worker.errors import classify, to_application_error
from edisc_worker.pipeline import CrashHooks, Pipeline


def _classified[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Translate any exception into a classified ``ApplicationError``. Unclassified errors are retried
    at most ``unclassified_max_attempts`` times, then become non-retryable (the unit fails loudly)."""

    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            cls = classify(exc)
            owner = args[0]
            if not isinstance(owner, Activities):
                raise TypeError("@_classified decorates Activities methods only") from exc
            exhausted = (
                cls is ErrorClass.UNCLASSIFIED
                and activity.info().attempt >= owner.settings.unclassified_max_attempts
            )
            raise to_application_error(exc, error_class=cls, final=exhausted) from exc

    return wrapper


def _ticking[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Heartbeat in the background while the activity runs, so a worker that dies (SIGKILL) is
    detected within the heartbeat timeout instead of start-to-close. ``collect_pages`` does not use
    this: it heartbeats per batch and per limiter wait, where it also checks cancel and the time box."""

    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        timeout = activity.info().heartbeat_timeout
        if not timeout:
            return await fn(*args, **kwargs)
        interval = max(timeout.total_seconds() / 3, 0.1)

        async def tick() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(interval)

        ticker = asyncio.create_task(tick())
        try:
            return await fn(*args, **kwargs)
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    return wrapper


@dataclass
class Activities:
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    settings: Settings
    connectors: Mapping[str, Connector]
    temporal: Client | None = None  # for the in-flight backstop (describe children)
    hooks: CrashHooks = field(default_factory=CrashHooks)

    def __post_init__(self) -> None:
        ensure_dummy_permitted(self.settings.env, self.connectors)

    # ------------------------------------------------------------------ helpers
    async def _context(
        self, tenant_id: uuid.UUID, job_id: uuid.UUID
    ) -> tuple[Pipeline, Connection]:
        async with tenant_tx(self.sessions, tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT c.id, c.source, c.external_org_id, c.config FROM collection_jobs j"
                        " JOIN connections c ON c.tenant_id = j.tenant_id AND c.id = j.connection_id WHERE j.id = :j"
                    ),
                    {"j": job_id},
                )
            ).one()
        connector = self.connectors[row.source]
        conn = Connection(tenant_id, row.id, row.source, row.external_org_id, dict(row.config))
        return Pipeline(self.sessions, self.s3, self.settings, connector, self.hooks), conn

    def _pipeline(self) -> Pipeline:
        """For calls that never touch the source (any connector satisfies the dataclass)."""
        return Pipeline(
            self.sessions, self.s3, self.settings, next(iter(self.connectors.values())), self.hooks
        )

    @staticmethod
    def _ids(ref: JobRef | UnitRef) -> tuple[uuid.UUID, uuid.UUID]:
        return uuid.UUID(ref.tenant_id), uuid.UUID(ref.job_id)

    # ------------------------------------------------------------------ job level
    @activity.defn(name="enumerate_units")
    @_classified
    @_ticking
    async def enumerate_units(self, ref: JobRef) -> int:
        tenant, job = self._ids(ref)
        pipeline, conn = await self._context(tenant, job)
        return await pipeline.enumerate_units(tenant_id=tenant, job_id=job, conn=conn)

    @activity.defn(name="job_overview")
    @_classified
    @_ticking
    async def job_overview(self, req: OverviewRequest) -> JobOverview:
        tenant, job = self._ids(req.job)
        pipeline = self._pipeline()
        state = await pipeline.job_state(tenant, job)
        units = await pipeline.startable_units(tenant, job, req.limit)
        return JobOverview(
            state.status.value,
            state.stop_reason,
            state.sealed,
            units.ready,
            units.remaining,
            units.next_retry_in,
        )

    @activity.defn(name="closed_children")
    @_classified
    @_ticking
    async def closed_children(self, req: ChildrenRequest) -> list[str]:
        """Backstop for a lost ``unit_finished`` signal: which of these units has no running child."""
        if self.temporal is None:
            raise RuntimeError("closed_children needs a Temporal client")
        closed: list[str] = []
        for key in req.unit_keys:
            handle = self.temporal.get_workflow_handle(unit_workflow_id(req.job.job_id, key))
            try:
                desc = await handle.describe()
            except RPCError as exc:
                if exc.status is not RPCStatusCode.NOT_FOUND:
                    raise
                closed.append(key)
                continue
            if desc.status is not WorkflowExecutionStatus.RUNNING:
                closed.append(key)
        return closed

    @activity.defn(name="request_stop")
    @_classified
    @_ticking
    async def request_stop(self, req: StopRequest) -> bool:
        tenant, job = self._ids(req.job)
        return await self._pipeline().request_stop(
            tenant_id=tenant, job_id=job, reason=req.reason, detail=req.detail, actor="workflow"
        )

    @activity.defn(name="pause_for_reauth")
    @_classified
    @_ticking
    async def pause_for_reauth(self, req: PauseRequest) -> int:
        tenant, job = self._ids(req.job)
        _, conn = await self._context(tenant, job)
        paused = await self._pipeline().pause_connection(
            tenant_id=tenant, connection_id=conn.connection_id, reason=req.reason
        )
        return len(paused)

    @activity.defn(name="finalize_job")
    @_classified
    @_ticking
    async def finalize_job(self, ref: JobRef) -> str:
        tenant, job = self._ids(ref)
        pipeline, _ = await self._context(tenant, job)  # the job's own connector decides its status
        return (await pipeline.finalize_job(tenant_id=tenant, job_id=job)).value

    # ------------------------------------------------------------------ unit level
    @activity.defn(name="collect_pages")
    @_classified
    async def collect_pages(self, req: CollectRequest) -> str:
        tenant, job = self._ids(req.unit)
        pipeline, conn = await self._context(tenant, job)
        outcome = await pipeline.collect_pages(
            tenant_id=tenant,
            job_id=job,
            unit_key=req.unit.unit_key,
            conn=conn,
            max_pages=req.max_pages,
            heartbeat=activity.heartbeat,
        )
        return outcome.value

    @activity.defn(name="finalize_unit")
    @_classified
    @_ticking
    async def finalize_unit(self, ref: UnitRef) -> str:
        tenant, job = self._ids(ref)
        pipeline, conn = await self._context(tenant, job)
        return await pipeline.finalize_unit(
            tenant_id=tenant, job_id=job, unit_key=ref.unit_key, conn=conn
        )

    @activity.defn(name="fail_unit")
    @_classified
    @_ticking
    async def fail_unit(self, req: UnitFailure) -> None:
        tenant, job = self._ids(req.unit)
        await self._pipeline().fail_unit(
            tenant_id=tenant,
            job_id=job,
            unit_key=req.unit.unit_key,
            error_type=req.error_type,
            error=req.error,
        )

    @activity.defn(name="defer_unit")
    @_classified
    @_ticking
    async def defer_unit(self, req: UnitFailure) -> str:
        tenant, job = self._ids(req.unit)
        return await self._pipeline().defer_unit(
            tenant_id=tenant,
            job_id=job,
            unit_key=req.unit.unit_key,
            error=f"{req.error_type}: {req.error}",
        )

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.enumerate_units,
            self.job_overview,
            self.closed_children,
            self.request_stop,
            self.pause_for_reauth,
            self.finalize_job,
            self.collect_pages,
            self.finalize_unit,
            self.fail_unit,
            self.defer_unit,
        ]
