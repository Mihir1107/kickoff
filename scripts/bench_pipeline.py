"""Throughput of the batch transaction path (no Temporal): one Pipeline.run over a dummy dataset.

    EDISC_ENV_FILE=.env.test uv run python scripts/bench_pipeline.py --conversations 5 --days 2 --messages 500

Prints messages/s and per-batch transaction statistics. Used to size the 50k acceptance test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy import text

from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_connectors_base.types import Connection
from edisc_core.ids import new_id
from edisc_core.settings import RateLimitConfig, Settings
from edisc_db.session import create_engine, create_tenant, session_factory, tenant_tx
from edisc_evidence.s3 import s3_client
from edisc_worker.pipeline import Pipeline


async def main(args: argparse.Namespace) -> None:
    settings = Settings()
    engine = create_engine(settings, "app")
    sessions = session_factory(engine)
    spec = DatasetSpec(
        seed=args.seed,
        conversations=args.conversations,
        days=args.days,
        messages_per_unit=args.messages,
        page_size=args.page_size,
        messy_pagination=False,
    )
    ds = Dataset(spec)
    tenant, matter, conn_id = new_id(), new_id(), new_id()
    await create_tenant(
        sessions,
        tenant_id=tenant,
        name="bench",
        subdomain=f"b-{secrets.token_hex(6)}",
        kms_key_ref="local:k",
    )
    async with tenant_tx(sessions, tenant) as s:
        await s.execute(
            text(
                "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES (:m, :t, 'M', :r)"
            ),
            {"m": matter, "t": tenant, "r": datetime.now(UTC) + timedelta(days=30)},
        )
        await s.execute(
            text(
                "INSERT INTO connections (id, tenant_id, source, external_org_id, status, config)"
                " VALUES (:c, :t, 'dummy', :w, 'active', CAST(:cfg AS jsonb))"
            ),
            {
                "c": conn_id,
                "t": tenant,
                "w": spec.workspace_id,
                "cfg": json.dumps({"spec": spec.model_dump(mode="json")}),
            },
        )
    redis = aioredis.from_url(settings.redis_url)
    limits = {
        k: RateLimitConfig(rate_per_second=100_000, burst=10_000) for k in settings.rate_limits
    }
    async with s3_client(settings) as s3:
        p = Pipeline(sessions, s3, settings, DummyConnector(RateLimiter(redis, limits)))
        conn = Connection(
            tenant, conn_id, "dummy", spec.workspace_id, {"spec": spec.model_dump(mode="json")}
        )
        job = new_id()
        await p.start_job(
            tenant_id=tenant,
            job_id=job,
            matter_id=matter,
            connection_id=conn_id,
            scopes=[
                scope_for_days(
                    "*", datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC), args.days
                )
            ],
            requested_by="bench",
        )
        started = time.perf_counter()
        status = await p.run(tenant_id=tenant, job_id=job, conn=conn, max_pages=50)
        elapsed = time.perf_counter() - started
    total = args.conversations * args.days * args.messages
    print(f"{status.value}: {total} messages in {elapsed:.1f}s = {total / elapsed:.0f} msg/s")
    await redis.aclose()
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=5)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--messages", type=int, default=500)
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    asyncio.run(main(ap.parse_args()))
