"""``ensure-job-reports`` (ADR 0018 §9): every sealed job gets a collection report, and a job still
without a COMPLETED report after ``EDISC_REPORT_MISSING_SECONDS`` opens a ``report_missing`` episode
with one alert.

- A sealed job of an open matter with no report at all gets one (actor ``system``, the default
  paper), and its workflow is started on the queue of this runtime. Failed and cancelled jobs
  included. A job whose report failed or was refused is NOT given another one automatically (a
  deterministic failure would repeat forever): its ``report_missing`` episode alerts a human, who
  regenerates it by hand (``POST /v1/jobs/{id}/reports``, with a reason).
- Discovery is cross-tenant through SECURITY DEFINER functions owned by the sweeper login (ids only);
  every write runs as the app role in the tenant's own transaction.
- One open ``report_missing`` episode per job (partial unique index), one alert per episode; the
  seal of a completed report closes it (``report_completed``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.contracts import report_task_queue
from edisc_worker.render_routing import workers_polling
from edisc_worker.reports import (
    close_episodes,
    create_report,
    open_episode,
    runtime_identity,
    start_report_workflow,
)


@dataclass
class EnsureResult:
    created: list[str] = field(default_factory=list)  # report ids
    missing_opened: list[str] = field(default_factory=list)  # job ids
    unroutable_opened: list[str] = field(default_factory=list)  # report ids
    unroutable_closed: int = 0


async def ensure_job_reports(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    client: Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID | None = None,
    limit: int = 200,
) -> EnsureResult:
    out = EnsureResult()
    identity = runtime_identity()
    async with sweeper_sessions() as session, session.begin():
        todo = (
            await session.execute(
                text("SELECT tenant_id, job_id FROM sealed_jobs_without_report(:n, :t)"),
                {"n": limit, "t": tenant_id},
            )
        ).all()
        missing = (
            await session.execute(
                text(
                    "SELECT tenant_id, job_id FROM jobs_missing_report("
                    "make_interval(secs => :age), :n, :t)"
                ),
                {"age": settings.report_missing_seconds, "n": limit, "t": tenant_id},
            )
        ).all()
    for row in todo:
        async with tenant_tx(sessions, row.tenant_id) as s:
            job = (
                await s.execute(
                    text(
                        "SELECT j.matter_id FROM collection_jobs j WHERE j.id = :j"
                        " AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.job_id = j.id)"
                        " FOR NO KEY UPDATE OF j"
                    ),
                    {"j": row.job_id},
                )
            ).first()
            if job is None:  # another run created it meanwhile
                continue
            created = await create_report(
                s, tenant_id=row.tenant_id, job_id=row.job_id, matter_id=job.matter_id,
                requested_by="system", identity=identity,
            )  # fmt: skip
        await start_report_workflow(client, settings, row.tenant_id, created.report_id, identity)
        out.created.append(str(created.report_id))
    for row in missing:
        async with tenant_tx(sessions, row.tenant_id) as s:
            opened = await open_episode(
                s, tenant_id=row.tenant_id, subject=("job_id", row.job_id), kind="report_missing",
                job_id=row.job_id,
                detail=f"no completed collection report {settings.report_missing_seconds:.0f}s"
                " after the job was sealed",
                message=f"job {row.job_id} has no completed collection report "
                f"{settings.report_missing_seconds:.0f}s after it was sealed",
            )  # fmt: skip
        if opened:
            out.missing_opened.append(str(row.job_id))
    await _check_routing(sweeper_sessions, sessions, client, settings, out, tenant_id, limit)
    return out


async def _check_routing(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    client: Client,
    settings: Settings,
    out: EnsureResult,
    tenant_id: uuid.UUID | None,
    limit: int,
) -> None:
    """Unroutable reports (the render rule, ADR 0015 §16): a report still ``requested`` after
    ``EDISC_RENDER_UNROUTABLE_SECONDS`` whose runtime's queue no worker polled opens an
    ``unroutable`` episode (one alert); a worker polling again closes it (``worker_available``),
    ``begin`` closes it too (``picked_up``)."""
    async with sweeper_sessions() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id, report_id, renderer_version, toolchain_id, unicode_version"
                    " FROM stale_requested_reports(make_interval(secs => :age), :n, :t)"
                ),
                {"age": settings.render_unroutable_seconds, "n": limit, "t": tenant_id},
            )
        ).all()
    max_age = timedelta(seconds=settings.render_poller_max_age_seconds)
    polled: dict[str, bool] = {}
    for row in rows:
        queue = report_task_queue(row.renderer_version, row.toolchain_id, row.unicode_version)
        if queue not in polled:
            polled[queue] = await workers_polling(client, queue, max_age)
        async with tenant_tx(sessions, row.tenant_id) as s:
            cur = (
                await s.execute(
                    text("SELECT status, job_id FROM reports WHERE id = :r FOR NO KEY UPDATE"),
                    {"r": row.report_id},
                )
            ).one()
            if cur.status != "requested":
                continue
            if polled[queue]:
                out.unroutable_closed += await close_episodes(
                    s, row.report_id, "worker_available", kind="unroutable"
                )
            elif await open_episode(
                s, tenant_id=row.tenant_id, subject=("report_id", row.report_id),
                kind="unroutable", job_id=cur.job_id, detail=f"no worker polls {queue}",
                message=f"report {row.report_id} is waiting for a worker of {queue}, and none"
                f" polled that queue in the last {int(max_age.total_seconds())} s",
            ):  # fmt: skip
                out.unroutable_opened.append(str(row.report_id))
