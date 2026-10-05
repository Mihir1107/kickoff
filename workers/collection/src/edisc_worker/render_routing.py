"""Unroutable renders (ADR 0015 §16): a render must never wait silently for a worker that does not exist.

Renders run on the queue of the versions they recorded (``render_task_queue``). A periodic check
(maintenance schedule ``check-render-routing``) looks at renders still ``requested`` after
``EDISC_RENDER_UNROUTABLE_SECONDS`` and asks Temporal (DescribeTaskQueue) whether any worker polled
the render's queue within ``EDISC_RENDER_POLLER_MAX_AGE_SECONDS``:

- no worker: an ``unroutable`` episode is opened (one alert per episode; the API shows the state);
- a worker again: the open episode is closed (``worker_available``); leaving ``requested`` closes it
  too (``picked_up``, in ``begin``'s transaction). A later loss of workers opens a NEW episode.

Cross-tenant: the sweeper login lists ids only (``stale_requested_renders``); every write runs as the
app role inside the render's tenant transaction. Failures are collected and raised together.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.contracts import render_task_queue
from edisc_worker.renders import close_episodes, open_episode


@dataclass
class RoutingResult:
    checked: int = 0
    opened: int = 0  # unroutable episodes opened (each with one alert)
    closed: int = 0  # unroutable episodes closed because a worker polls again
    queues_without_workers: list[str] = field(default_factory=list)


class RoutingCheckError(ExceptionGroup[Exception]):
    pass


async def workers_polling(client: Client, queue: str, max_age: timedelta) -> bool:
    """Has any worker polled ``queue`` for workflow tasks within ``max_age``?"""
    resp = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=client.namespace,
            task_queue=TaskQueue(name=queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        )
    )
    since = datetime.now(UTC) - max_age
    return any(p.last_access_time.ToDatetime(tzinfo=UTC) >= since for p in resp.pollers)


async def check_render_routing(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    client: Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID | None = None,
    limit: int = 500,
) -> RoutingResult:
    async with sweeper_sessions() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id, render_id, renderer_version, unicode_version, tzdata_version"
                    " FROM stale_requested_renders(make_interval(secs => :age), :n, :t)"
                ),
                {"age": settings.render_unroutable_seconds, "n": limit, "t": tenant_id},
            )
        ).all()
    result = RoutingResult()
    polled: dict[str, bool] = {}
    failures: list[Exception] = []
    max_age = timedelta(seconds=settings.render_poller_max_age_seconds)
    for row in rows:
        queue = render_task_queue(row.renderer_version, row.unicode_version, row.tzdata_version)
        try:
            if queue not in polled:
                polled[queue] = await workers_polling(client, queue, max_age)
                if not polled[queue]:
                    result.queues_without_workers.append(queue)
            async with tenant_tx(sessions, row.tenant_id) as s:
                cur = (
                    await s.execute(
                        text("SELECT status, job_id FROM renders WHERE id = :r FOR NO KEY UPDATE"),
                        {"r": row.render_id},
                    )
                ).one()
                if cur.status != "requested":
                    continue  # picked up meanwhile (begin closed any episode)
                result.checked += 1
                if polled[queue]:
                    result.closed += await close_episodes(
                        s, row.render_id, "worker_available", kind="unroutable"
                    )
                elif await open_episode(
                    s, tenant_id=row.tenant_id, render_id=row.render_id, job_id=cur.job_id,
                    kind="unroutable", detail=f"no worker polls {queue}",
                    message=f"render {row.render_id} is waiting for a worker of {queue}, and none"
                    f" polled that queue in the last {int(max_age.total_seconds())} s",
                ):  # fmt: skip
                    result.opened += 1
        except Exception as exc:  # noqa: BLE001 - collected and re-raised below, never swallowed
            exc.add_note(f"checking routing of render {row.render_id} of tenant {row.tenant_id}")
            failures.append(exc)
    if failures:
        raise RoutingCheckError(f"{len(failures)} of {len(rows)} routing checks failed", failures)
    return result
