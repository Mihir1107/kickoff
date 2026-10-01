"""Collection worker process: ``python -m edisc_worker [--source dummy ...]``.

One Temporal worker per source, on task queue ``collect-{source}``, sharing one DB pool, S3 client and
Redis limiter. At startup, journaled token refreshes are reconciled (ADR 0009) before any activity runs.
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
from edisc_worker.contracts import task_queue
from edisc_worker.workflows import CollectionJobWorkflow, CollectUnitWorkflow

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
) -> AsyncIterator[Activities]:
    engine = create_engine(settings, "app")
    sweeper = create_engine(settings, "sweeper", pool_size=1)
    redis = aioredis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=5)
    try:
        sessions = session_factory(engine)
        result = await reconcile_token_refreshes(session_factory(sweeper), sessions)
        log.info("token refresh journal reconciled", **result)
        limiter = RateLimiter(redis, settings.rate_limits)
        async with s3_client(settings) as s3:
            yield Activities(sessions, s3, settings, build_connectors(limiter, sources), client)
    finally:
        await redis.aclose()
        await sweeper.dispose()
        await engine.dispose()


async def run(sources: Sequence[str]) -> None:
    settings = Settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    async with activities_for(settings, sources, client) as acts:
        workers = [
            Worker(
                client,
                task_queue=task_queue(source),
                workflows=WORKFLOWS,
                activities=acts.all(),
                max_concurrent_activities=settings.max_units_in_flight * 2,
            )
            for source in sources
        ]
        log.info("worker started", task_queues=[task_queue(s) for s in sources])
        await asyncio.gather(*(w.run() for w in workers))


def main() -> None:
    ap = argparse.ArgumentParser(prog="edisc_worker")
    ap.add_argument("--source", action="append", dest="sources", help="repeatable; default dummy")
    args = ap.parse_args()
    configure_logging()
    asyncio.run(run(args.sources or ["dummy"]))


if __name__ == "__main__":
    main()
