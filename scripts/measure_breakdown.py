"""Storage and throughput breakdown of one collection job (measurement only; changes nothing).

    make test-env-up
    EDISC_ENV_FILE=.env.test uv run python scripts/measure_breakdown.py --out breakdown.json

Run on a FRESH ephemeral test stack (other data would blur the before/after deltas):

A. Storage, through Temporal with real worker processes (no kills): before/after deltas of
   - every edisc table split into heap, TOAST and each index; WAL bytes generated;
   - the temporal and temporal_visibility databases; workflow history sizes;
   - MinIO: logical bytes and object counts per kind (pages, files, anchors), on-disk bytes of the
     evidence/staging buckets (``du`` inside the MinIO container), so per-object overhead shows;
   - dummy attachment bytes (file objects) separated from per-message overhead;
   - compressibility of a sample of page objects (zlib, lzma).
B. Throughput, in process (no Temporal) on a second tenant: wall time per page by stage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import lzma
import secrets
import subprocess
import sys
import tempfile
import time
import zlib
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

import edisc_worker.pipeline as pipeline_mod
from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_connectors_base.types import Connection
from edisc_core.ids import new_id
from edisc_core.settings import RateLimitConfig, Settings
from edisc_db.session import create_engine, create_tenant, session_factory, tenant_tx
from edisc_evidence.s3 import s3_client
from edisc_evidence.writer import EvidenceWriter
from edisc_worker.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import resume_soak  # noqa: E402  (same directory; the soak driver runs part A)

COMPOSE = ["docker", "compose", "-p", "edisc-test", "-f", str(ROOT / "infra/docker-compose.yml"),
           "--env-file", str(ROOT / ".env.test")]  # fmt: skip


# ---------------------------------------------------------------- snapshots
async def pg_snapshot(superuser: AsyncEngine) -> dict[str, Any]:
    async with superuser.connect() as c:
        rel = (
            await c.execute(
                text(
                    "SELECT c.relname AS name, pg_relation_size(c.oid) AS heap,"
                    " coalesce(pg_total_relation_size(c.reltoastrelid), 0) AS toast"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'edisc' AND c.relkind = 'r'"
                )
            )
        ).all()
        idx = (
            await c.execute(
                text(
                    "SELECT t.relname AS tbl, i.relname AS name, pg_relation_size(i.oid) AS size"
                    " FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid"
                    " JOIN pg_class t ON t.oid = x.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace"
                    " WHERE n.nspname = 'edisc'"
                )
            )
        ).all()
        dbs = (
            await c.execute(
                text(
                    "SELECT datname, pg_database_size(datname) FROM pg_database"
                    " WHERE datname IN ('edisc', 'temporal', 'temporal_visibility')"
                )
            )
        ).all()
        wal = (await c.execute(text("SELECT pg_current_wal_lsn()::text"))).scalar_one()
    return {
        "tables": {r.name: {"heap": r.heap, "toast": r.toast} for r in rel},
        "indexes": {f"{r.tbl}.{r.name}": r.size for r in idx},
        "databases": {r[0]: r[1] for r in dbs},
        "wal_lsn": wal,
    }


def minio_du() -> dict[str, int]:
    out = subprocess.run(  # noqa: S603 - fixed argv, no user input
        [*COMPOSE, "exec", "-T", "minio", "sh", "-c", "du -sk /data/* 2>/dev/null"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {
        line.split()[1].rsplit("/", 1)[-1]: int(line.split()[0]) * 1024 for line in out.splitlines()
    }


async def wal_bytes(superuser: AsyncEngine, start: str, end: str) -> int:
    async with superuser.connect() as c:
        value: int = (
            await c.execute(
                text(
                    "SELECT pg_wal_lsn_diff(CAST(CAST(:e AS text) AS pg_lsn), CAST(CAST(:s AS text) AS pg_lsn))::bigint"
                ),
                {"e": end, "s": start},
            )
        ).scalar_one()
    return value


async def objects(s3: S3Client, bucket: str, prefix: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    paginator = s3.get_paginator("list_object_versions")
    async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        found.extend(page.get("Versions", []))
    return found


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    tables = {
        name: {
            "heap": after["tables"][name]["heap"] - before["tables"].get(name, {}).get("heap", 0),
            "toast": after["tables"][name]["toast"]
            - before["tables"].get(name, {}).get("toast", 0),
        }
        for name in after["tables"]
    }
    indexes = {k: v - before["indexes"].get(k, 0) for k, v in after["indexes"].items()}
    dbs = {k: v - before["databases"].get(k, 0) for k, v in after["databases"].items()}
    return {
        "tables": {k: v for k, v in tables.items() if v["heap"] or v["toast"]},
        "indexes": {k: v for k, v in indexes.items() if v},
        "databases": dbs,
    }


# ---------------------------------------------------------------- part B timing
class Timer:
    def __init__(self) -> None:
        self.seconds: dict[str, float] = defaultdict(float)
        self.calls: dict[str, int] = defaultdict(int)

    def wrap[**P, R](self, name: str, fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        async def timed(*args: P.args, **kwargs: P.kwargs) -> R:
            started = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                self.seconds[name] += time.perf_counter() - started
                self.calls[name] += 1

        return timed

    def wrap_sync[**P, R](self, name: str, fn: Callable[P, R]) -> Callable[P, R]:
        def timed(*args: P.args, **kwargs: P.kwargs) -> R:
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.seconds[name] += time.perf_counter() - started
                self.calls[name] += 1

        return timed


class TimedConnector(DummyConnector):
    def __init__(self, limiter: RateLimiter, timer: Timer) -> None:
        super().__init__(limiter)
        self.timer = timer

    async def fetch(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:  # type: ignore[override]
        it = super().fetch(*args, **kwargs).__aiter__()
        while True:
            started = time.perf_counter()
            try:
                batch = await it.__anext__()
            except StopAsyncIteration:
                return
            finally:
                self.timer.seconds["source_fetch"] += time.perf_counter() - started
            self.timer.calls["source_fetch"] += 1
            yield batch


async def setup_tenant(sessions: Any, sp: DatasetSpec) -> tuple[Any, Any, Any]:
    tenant, matter, conn_id = new_id(), new_id(), new_id()
    await create_tenant(
        sessions,
        tenant_id=tenant,
        name="m",
        subdomain=f"m-{secrets.token_hex(6)}",
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
                "w": sp.workspace_id,
                "cfg": json.dumps({"spec": sp.model_dump(mode="json"), "epoch": 0}),
            },
        )
    return tenant, matter, conn_id


async def throughput(
    settings: Settings, sessions: Any, s3: S3Client, sp: DatasetSpec, redis: Any
) -> dict[str, Any]:
    timer = Timer()
    limits = {
        k: RateLimitConfig(rate_per_second=100_000, burst=10_000) for k in settings.rate_limits
    }
    connector = TimedConnector(RateLimiter(redis, limits), timer)
    originals = {
        name: getattr(pipeline_mod, name)
        for name in ("persist", "append_batch", "load_prior", "anchor_if_due")
    }
    sync_originals = {
        name: getattr(pipeline_mod, name)
        for name in ("normalize_messages_page", "normalize_directory_page")
    }
    writer_originals = (EvidenceWriter.write_page, EvidenceWriter.write_file)
    pipe_originals = (
        Pipeline.process_batch,
        Pipeline._files,
        Pipeline._link_batch,
        Pipeline.finalize_unit,
    )
    for name, fn in originals.items():
        setattr(pipeline_mod, name, timer.wrap(name, fn))
    for name, fn in sync_originals.items():
        setattr(pipeline_mod, name, timer.wrap_sync(name, fn))
    EvidenceWriter.write_page = timer.wrap("evidence_page", writer_originals[0])  # type: ignore[method-assign]
    EvidenceWriter.write_file = timer.wrap("evidence_file", writer_originals[1])  # type: ignore[method-assign]
    Pipeline.process_batch = timer.wrap("process_batch", pipe_originals[0])  # type: ignore[method-assign]
    Pipeline._files = timer.wrap("files_total", pipe_originals[1])  # type: ignore[method-assign]
    Pipeline._link_batch = timer.wrap("link_batch", pipe_originals[2])  # type: ignore[method-assign]
    Pipeline.finalize_unit = timer.wrap("finalize_unit", pipe_originals[3])  # type: ignore[method-assign]
    try:
        ds = Dataset(sp)
        tenant, matter, conn_id = await setup_tenant(sessions, sp)
        conn = Connection(
            tenant,
            conn_id,
            "dummy",
            sp.workspace_id,
            {"spec": sp.model_dump(mode="json"), "epoch": 0},
        )
        job = new_id()
        p = Pipeline(sessions, s3, settings, connector)
        await p.start_job(
            tenant_id=tenant, job_id=job, matter_id=matter, connection_id=conn_id,
            scopes=[scope_for_days("*", datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC), sp.days)],
            requested_by="measure",
        )  # fmt: skip
        started = time.perf_counter()
        status = await p.run(tenant_id=tenant, job_id=job, conn=conn, max_pages=50)
        wall = time.perf_counter() - started
    finally:
        for name, fn in originals.items():
            setattr(pipeline_mod, name, fn)
        for name, fn in sync_originals.items():
            setattr(pipeline_mod, name, fn)
        EvidenceWriter.write_page, EvidenceWriter.write_file = writer_originals  # type: ignore[method-assign]
        (Pipeline.process_batch, Pipeline._files, Pipeline._link_batch, Pipeline.finalize_unit) = (
            pipe_originals  # type: ignore[method-assign]
        )
    return {
        "status": status.value,
        "wall_seconds": wall,
        "seconds": dict(timer.seconds),
        "calls": dict(timer.calls),
    }


# ---------------------------------------------------------------- main
async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=5)
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--messages-per-unit", type=int, default=500)
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    settings = Settings()
    app, su = create_engine(settings, "app"), create_engine(settings, "superuser")
    sessions = session_factory(app)
    redis = aioredis.from_url(settings.redis_url)
    temporal = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    result: dict[str, Any] = {"args": {k: str(v) for k, v in vars(args).items()}}
    async with s3_client(settings) as s3:
        cfg = resume_soak.SoakConfig(
            conversations=args.conversations, days=args.days, messages_per_unit=args.messages_per_unit,
            page_size=args.page_size, kills=0, kill_all=False, workdir=Path(tempfile.gettempdir()) / "edisc-measure",
        )  # fmt: skip
        before, du_before = await pg_snapshot(su), minio_du()
        started = time.perf_counter()
        soak = await resume_soak.soak(
            cfg, sessions=sessions, s3=s3, settings=settings, temporal=temporal,
            limiter=RateLimiter(redis, settings.rate_limits),
        )  # fmt: skip
        temporal_wall = time.perf_counter() - started
        await asyncio.sleep(5)  # let Temporal flush visibility/history writes
        after, du_after = await pg_snapshot(su), minio_du()
        storage = diff(before, after)
        storage["wal_bytes"] = await wal_bytes(su, before["wal_lsn"], after["wal_lsn"])
        storage["minio_disk"] = {k: du_after.get(k, 0) - du_before.get(k, 0) for k in du_after}
        tenant, job = str(soak.tenant_id), str(soak.job_id)
        pages = await objects(s3, settings.s3_evidence_bucket, f"t/{tenant}/jobs/{job}/pages/")
        files = await objects(s3, settings.s3_evidence_bucket, f"t/{tenant}/files/")
        anchors = await objects(s3, settings.s3_evidence_bucket, f"custody-anchors/{tenant}/")
        storage["minio_logical"] = {
            kind: {"objects": len(objs), "bytes": sum(o["Size"] for o in objs)}
            for kind, objs in (("pages", pages), ("files", files), ("anchors", anchors))
        }
        sample = pages[:: max(1, len(pages) // 20)][:20]
        raw = zl = lz = 0
        for o in sample:
            body = await (
                await s3.get_object(
                    Bucket=settings.s3_evidence_bucket, Key=o["Key"], VersionId=o["VersionId"]
                )
            )["Body"].read()
            raw, zl, lz = (
                raw + len(body),
                zl + len(zlib.compress(body, 6)),
                lz + len(lzma.compress(body, preset=6)),
            )
        storage["page_compression_sample"] = {
            "pages": len(sample),
            "raw": raw,
            "zlib6": zl,
            "lzma6": lz,
        }
        sizes = []
        async for wf in temporal.list_workflows(f"WorkflowId STARTS_WITH '{job}'"):
            d = await temporal.get_workflow_handle(wf.id, run_id=wf.run_id).describe()
            info = d.raw_description.workflow_execution_info
            sizes.append((info.history_length, info.history_size_bytes))
        storage["temporal_histories"] = {
            "runs": len(sizes),
            "events": sum(s[0] for s in sizes),
            "bytes": sum(s[1] for s in sizes),
        }
        result["storage"] = storage
        result["soak"] = {
            "status": soak.status,
            "seconds": temporal_wall,
            "problems": soak.problems,
        }
        result["messages"] = cfg.messages
        result["throughput_in_process"] = await throughput(
            settings, sessions, s3, cfg.spec(), redis
        )
    await redis.aclose()
    await app.dispose()
    await su.dispose()
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
