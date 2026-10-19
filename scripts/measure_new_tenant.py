"""A NEW tenant's first collection, in the statistics state of a large shared database (ADR 0015
§24.20; docs/runs/2026-10-19-export-validation-stale-statistics.md).

    EDISC_ENV_FILE=.env.test uv run python scripts/measure_new_tenant.py --messages 5500 --out x.json

The state: ``items``, ``job_items`` and ``item_derivations`` are analyzed WITHOUT the new tenant,
and autovacuum is held off on them for the run (in production a large table's 2 % autoanalyze
threshold is not reached by one tenant's first collection; restored at the end, whatever happens).
Then one ``Pipeline.run`` of a dummy dataset for a tenant created here, and a JSON line: wall
time, items the job inserted, tuples read from ``items`` (index fetches + sequential reads, from
``pg_stat_user_tables``), and the ANALYZEs of ``items`` during the run.

Runs against the test stack only (`make test-env-up`); the dummy connector is refused elsewhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
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

TABLES = ("items", "job_items", "item_derivations")


async def _counters(su: asyncpg.Connection) -> dict[str, int]:
    await asyncio.sleep(1.5)  # backends flush their table statistics at most once a second
    row = await su.fetchrow(
        "SELECT coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) AS reads,"
        " analyze_count + autoanalyze_count AS analyzes FROM pg_stat_user_tables"
        " WHERE schemaname = 'edisc' AND relname = 'items'"
    )
    if row is None:
        raise RuntimeError("edisc.items has no statistics row")
    return {"reads": int(row["reads"]), "analyzes": int(row["analyzes"])}


async def main(args: argparse.Namespace) -> None:
    settings = Settings()
    su = await asyncpg.connect(settings.pg_dsn("superuser"))
    for table in TABLES:  # the new tenant is in no statistic; the run never triggers autoanalyze
        await su.execute(f"ANALYZE edisc.{table}")
        await su.execute(f"ALTER TABLE edisc.{table} SET (autovacuum_enabled = false)")
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
    tenant, matter, conn_id, job = new_id(), new_id(), new_id(), new_id()
    record: dict[str, Any] = {"code": args.label, "spec": spec.model_dump(mode="json")}
    try:
        await create_tenant(
            sessions, tenant_id=tenant, name="measure", subdomain=f"m-{secrets.token_hex(6)}",
            kms_key_ref="local:k",
        )  # fmt: skip
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
                {"c": conn_id, "t": tenant, "w": spec.workspace_id,
                 "cfg": json.dumps({"spec": spec.model_dump(mode="json")})},
            )  # fmt: skip
        redis = aioredis.from_url(settings.redis_url)
        limits = {
            k: RateLimitConfig(rate_per_second=100_000, burst=10_000) for k in settings.rate_limits
        }
        async with s3_client(settings) as s3:
            p = Pipeline(sessions, s3, settings, DummyConnector(RateLimiter(redis, limits)))
            conn = Connection(
                tenant, conn_id, "dummy", spec.workspace_id, {"spec": spec.model_dump(mode="json")}
            )
            await p.start_job(
                tenant_id=tenant, job_id=job, matter_id=matter, connection_id=conn_id,
                scopes=[scope_for_days(
                    "*", datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC), args.days
                )],
                requested_by="measure",
            )  # fmt: skip
            c0 = await _counters(su)
            started = time.perf_counter()
            status = await p.run(tenant_id=tenant, job_id=job, conn=conn, max_pages=50)
            wall = time.perf_counter() - started
            c1 = await _counters(su)
        await redis.aclose()
        async with tenant_tx(sessions, tenant) as s:
            inserted = (
                await s.execute(text("SELECT count(*) FROM items WHERE job_id = :j"), {"j": job})
            ).scalar_one()
        record.update(
            status=status.value, wall_s=round(wall, 1), items_inserted=int(inserted),
            items_tuples_read=c1["reads"] - c0["reads"],
            reads_per_item=round((c1["reads"] - c0["reads"]) / max(1, inserted), 1),
            items_analyzes=c1["analyzes"] - c0["analyzes"],
            us_per_item=round(wall * 1e6 / max(1, inserted)),
        )  # fmt: skip
    finally:
        for table in TABLES:
            await su.execute(f"ALTER TABLE edisc.{table} RESET (autovacuum_enabled)")
        await su.close()
        await engine.dispose()
    line = json.dumps(record)
    print(line)
    if args.out:
        await asyncio.to_thread(_append, args.out, line)


def _append(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=10)
    ap.add_argument("--days", type=int, default=6)
    ap.add_argument("--messages", type=int, default=5500)
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--label", default="current")
    ap.add_argument("--out", default="")
    asyncio.run(main(ap.parse_args()))
