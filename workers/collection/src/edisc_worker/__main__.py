"""Collection worker process: ``python -m edisc_worker [--source dummy ...]``.

One Temporal worker per source, on task queue ``collect-{source}``, sharing one DB pool, S3 client and
Redis limiter. At startup, journaled token refreshes are reconciled (ADR 0009) before any activity runs.
``--maintenance`` also runs the ``maintenance`` queue (sweepers) and creates/updates their schedules.
``--exports`` also runs the ``exports`` queue: hash, lock and validate uploaded Slack exports (ADR 0014).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence

import redis.asyncio as aioredis
from temporalio.client import Client
from temporalio.worker import Worker

from edisc_connector_dummy.connector import DummyConnector
from edisc_connectors_base.protocol import Connector
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.logs import configure_logging, get_logger
from edisc_core.settings import Settings
from edisc_db.connection_tokens import reconcile_token_refreshes
from edisc_db.session import create_engine, session_factory
from edisc_evidence.s3 import s3_client
from edisc_worker.activities import Activities
from edisc_worker.contracts import EXPORTS_QUEUE, MAINTENANCE_QUEUE, task_queue
from edisc_worker.exports import ExportActivities
from edisc_worker.maintenance import MaintenanceActivities, ensure_schedules
from edisc_worker.workflows import (
    CollectionJobWorkflow,
    CollectUnitWorkflow,
    ExportIngestWorkflow,
    MaintenanceWorkflow,
)

log = get_logger("edisc_worker")

WORKFLOWS = [CollectionJobWorkflow, CollectUnitWorkflow]


def build_connectors(limiter: RateLimiter, sources: Sequence[str]) -> dict[str, Connector]:
    available: dict[str, Connector] = {"dummy": DummyConnector(limiter)}
    unknown = set(sources) - available.keys()
    if unknown:
        raise SystemExit(f"no connector for source(s): {sorted(unknown)}")
    return {s: available[s] for s in sources}


@contextlib.asynccontextmanager
async def activities_for(
    settings: Settings, sources: Sequence[str], client: Client
) -> AsyncIterator[tuple[Activities, MaintenanceActivities]]:
    engine = create_engine(settings, "app")
    sweeper = create_engine(settings, "sweeper", pool_size=1)
    redis = aioredis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=5)
    try:
        sessions = session_factory(engine)
        result = await reconcile_token_refreshes(session_factory(sweeper), sessions)
        log.info("token refresh journal reconciled", **result)
        limiter = RateLimiter(redis, settings.rate_limits)
        async with s3_client(settings) as s3:
            yield (
                Activities(sessions, s3, settings, build_connectors(limiter, sources), client),
                MaintenanceActivities(session_factory(sweeper), sessions, s3, settings),
            )
    finally:
        await redis.aclose()
        await sweeper.dispose()
        await engine.dispose()


async def run(
    sources: Sequence[str], *, maintenance: bool, exports: bool = False, queue: str | None = None
) -> None:
    settings = Settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    async with activities_for(settings, sources, client) as (acts, sweeps):
        workers = [
            Worker(
                client,
                task_queue=queue or task_queue(source),
                workflows=WORKFLOWS,
                activities=acts.all(),
                max_concurrent_activities=settings.max_units_in_flight * 2,
            )
            for source in sources
        ]
        if maintenance:
            workers.append(
                Worker(
                    client,
                    task_queue=MAINTENANCE_QUEUE,
                    workflows=[MaintenanceWorkflow],
                    activities=sweeps.all(),
                )
            )
            log.info("sweeper schedules ensured", schedules=await ensure_schedules(client))
        if exports:
            workers.append(
                Worker(
                    client,
                    task_queue=EXPORTS_QUEUE,
                    workflows=[ExportIngestWorkflow],
                    activities=ExportActivities(acts.sessions, acts.s3, settings).all(),
                )
            )
        log.info("worker started", task_queues=[w.task_queue for w in workers])
        await asyncio.gather(*(w.run() for w in workers))


def main() -> None:
    ap = argparse.ArgumentParser(prog="edisc_worker")
    ap.add_argument("--source", action="append", dest="sources", help="repeatable; default dummy")
    ap.add_argument("--maintenance", action="store_true", help="also run sweepers and schedules")
    ap.add_argument("--exports", action="store_true", help="also hash, lock and validate exports")
    ap.add_argument("--queue", help="task queue override (one source only; tests and soak runs)")
    args = ap.parse_args()
    sources = args.sources or ["dummy"]
    if args.queue and len(sources) != 1:
        ap.error("--queue needs exactly one --source")
    configure_logging()
    asyncio.run(run(sources, maintenance=args.maintenance, exports=args.exports, queue=args.queue))


if __name__ == "__main__":
    main()
