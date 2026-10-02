"""Resume soak (ADR 0012 section 8): a job over Temporal with real worker PROCESSES that get SIGKILLed.

    EDISC_ENV_FILE=.env.test uv run python scripts/resume_soak.py --messages 1000000 --workers 3 --kills 10

Dataset: ``conversations x days x messages_per_unit`` dummy messages (``--messages`` picks
messages_per_unit for the given conversations/days). Workers are ``python -m edisc_worker --queue ...``
subprocesses. While the job runs, a random worker is SIGKILLed ``--kills`` times (and replaced), and
once ALL workers are killed together and restarted. Then the job must be complete and exact:

- job status ``completed``, every unit ``done`` + ``matched``, collected == expected per unit;
- zero duplicate items, every job link backed by a custody event, no pending evidence;
- the custody chain (and its WORM anchors and seal) verifies.

The integration test ``tests/integration/acceptance/test_resume_50k.py`` runs this at 50k and also
compares every derived record with the dataset oracle. Results of manual runs go in ``docs/runs/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import secrets
import shutil
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.log import verify_chain
from edisc_db.session import create_tenant, tenant_tx
from edisc_worker.contracts import JobInput, RunConfig
from edisc_worker.pipeline import Pipeline
from edisc_worker.workflows import CollectionJobWorkflow

ROOT = Path(__file__).resolve().parents[1]
# measured (docs/runs/2026-10-01-storage-throughput-breakdown.md): ~6.9 KB per message at rest across
# Postgres, MinIO and Temporal; budget 1.5x that, plus WAL headroom (max_wal_size 1 GB, peaks above it),
# and require that much on top of a free-space floor that must remain AFTER the run
BYTES_PER_MESSAGE = 7_000
GROWTH_MARGIN = 1.5
WAL_HEADROOM_BYTES = 2 * 1024**3
FLOOR_AFTER_RUN_BYTES = 8 * 1024**3
FAST_LIMITS = {
    k: {"rate_per_second": 2000, "burst": 200}
    for k in ("dummy.fetch", "dummy.expected_count", "dummy.directory", "dummy.file")
}


@dataclass
class SoakConfig:
    conversations: int = 10
    days: int = 10
    messages_per_unit: int = 500
    page_size: int = 200
    workers: int = 3
    kills: int = 4
    kill_all: bool = True
    seed: int = 12
    timeout_seconds: float = 1500
    workdir: Path = field(default_factory=lambda: Path("soak-logs"))
    run: RunConfig = field(
        default_factory=lambda: RunConfig(
            max_units_in_flight=8,
            pages_per_activity=5,
            job_poll_seconds=5,
            heartbeat_timeout_seconds=10,
            retry_initial_seconds=0.5,
            retry_max_seconds=5,
        )
    )

    @property
    def messages(self) -> int:
        return self.conversations * self.days * self.messages_per_unit

    def spec(self) -> DatasetSpec:
        return DatasetSpec(
            seed=self.seed,
            conversations=self.conversations,
            days=self.days,
            messages_per_unit=self.messages_per_unit,
            page_size=self.page_size,
        )


@dataclass
class SoakResult:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    status: str
    seconds: float
    kills: list[str]
    problems: list[str]


class WorkerPool:
    def __init__(self, queue: str, workdir: Path, env: dict[str, str]) -> None:
        self.queue, self.workdir, self.env = queue, workdir, env
        self.procs: list[asyncio.subprocess.Process] = []
        self.started = 0

    async def spawn(self) -> None:
        self.started += 1
        log = (self.workdir / f"worker-{self.started}.log").open("wb")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "edisc_worker",
            "--queue",
            self.queue,
            cwd=ROOT,
            env=self.env,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        log.close()
        self.procs.append(proc)

    async def kill(self, proc: asyncio.subprocess.Process) -> None:
        proc.send_signal(signal.SIGKILL)
        await proc.wait()
        self.procs.remove(proc)

    async def stop_all(self) -> None:
        for proc in list(self.procs):
            if proc.returncode is None:
                await self.kill(proc)


async def _counts(
    sessions: async_sessionmaker[AsyncSession], tenant: uuid.UUID, job: uuid.UUID
) -> Any:
    async with tenant_tx(sessions, tenant) as s:
        return (
            await s.execute(
                text(
                    "SELECT"
                    " (SELECT count(*) FROM job_items WHERE job_id = :j) AS links,"
                    " (SELECT count(*) - count(DISTINCT idempotency_key) FROM items WHERE tenant_id = :t) AS dupes,"
                    " (SELECT count(*) FROM evidence_objects WHERE job_id = :j AND state = 'pending') AS pending,"
                    " (SELECT count(*) FROM job_items ji LEFT JOIN custody_events ce ON ce.id = ji.custody_event_id"
                    "   WHERE ji.job_id = :j AND ce.id IS NULL) AS dangling,"
                    " (SELECT count(*) FROM work_units WHERE job_id = :j AND kind = 'conversation_day'"
                    "   AND (status <> 'done' OR recon_status <> 'matched' OR collected_count <> expected_count))"
                    "   AS bad_units,"
                    " (SELECT coalesce(sum(collected_count), 0) FROM work_units WHERE job_id = :j) AS collected"
                ),
                {"j": job, "t": tenant},
            )
        ).one()


async def soak(
    cfg: SoakConfig,
    *,
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    temporal: Client,
    limiter: RateLimiter,
) -> SoakResult:
    rng = random.Random(cfg.seed)
    sp = cfg.spec()
    ds = Dataset(sp)
    tenant, matter, conn_id, job = new_id(), new_id(), new_id(), new_id()
    await create_tenant(
        sessions,
        tenant_id=tenant,
        name="soak",
        subdomain=f"soak-{secrets.token_hex(6)}",
        kms_key_ref="local:k",
    )
    async with tenant_tx(sessions, tenant) as s:
        await s.execute(
            text(
                "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES (:m, :t, 'Soak', :r)"
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
    first = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
    await Pipeline(sessions, s3, settings, DummyConnector(limiter)).start_job(
        tenant_id=tenant,
        job_id=job,
        matter_id=matter,
        connection_id=conn_id,
        scopes=[scope_for_days("*", first, ds.n_days(0))],
        requested_by="soak",
    )
    queue = f"collect-dummy-soak-{job.hex[:12]}"
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "EDISC_RATE_LIMITS": json.dumps(FAST_LIMITS),
        "PYTHONUNBUFFERED": "1",
    }
    pool = WorkerPool(queue, cfg.workdir, env)
    for _ in range(cfg.workers):
        await pool.spawn()
    started = time.monotonic()
    handle = await temporal.start_workflow(
        CollectionJobWorkflow.run,
        JobInput(str(tenant), str(job), cfg.run),
        id=str(job),
        task_queue=queue,
    )
    result_task = asyncio.ensure_future(handle.result())
    kills: list[str] = []
    try:
        # spread the kills over the job's PROGRESS (links committed), not wall time: a faster or slower
        # machine must not let the job finish before every kill fired (a timing-based plan was flaky)
        plan = ["one"] * cfg.kills + (["all"] if cfg.kill_all else [])
        rng.shuffle(plan)
        for n, kind in enumerate(plan, start=1):
            target = int(cfg.messages * (n - rng.uniform(0.0, 0.5)) / (len(plan) + 1))
            while True:
                done, _ = await asyncio.wait({result_task}, timeout=0.2)
                if done:
                    break
                counts = await _counts(sessions, tenant, job)
                if counts.links >= target:
                    break
            if done:
                break
            if kind == "all":
                await pool.stop_all()
                kills.append(f"ALL at {counts.links} links")
                await asyncio.sleep(rng.uniform(0.5, 3))
                for _ in range(cfg.workers):
                    await pool.spawn()
            else:
                victim = rng.choice(pool.procs)
                await pool.kill(victim)
                kills.append(f"pid {victim.pid} at {counts.links} links")
                await pool.spawn()
        status = await asyncio.wait_for(
            result_task, timeout=cfg.timeout_seconds - (time.monotonic() - started)
        )
    finally:
        if not result_task.done():
            result_task.cancel()
        await pool.stop_all()
    elapsed = time.monotonic() - started
    problems: list[str] = []
    counts = await _counts(sessions, tenant, job)
    if status != "completed":
        problems.append(f"status {status}")
    for name in ("dupes", "pending", "dangling", "bad_units"):
        if getattr(counts, name):
            problems.append(f"{name}={getattr(counts, name)}")
    expected_total = sum(
        ds.expected_count(c.id, d, 0) for c in ds.conversations() for d in range(ds.n_days(0))
    )
    if counts.collected != expected_total:
        problems.append(f"collected {counts.collected} != expected {expected_total}")
    report = await verify_chain(sessions, s3, settings, tenant_id=tenant, stream_id=job)
    if not report.ok:
        problems.append(f"chain: {report.errors[:3]}")
    return SoakResult(tenant, job, status, elapsed, kills, problems)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--messages", type=int, default=50_000)
    ap.add_argument("--conversations", type=int, default=10)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--kills", type=int, default=4)
    ap.add_argument("--seed", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=6 * 3600)
    ap.add_argument("--workdir", type=Path, default=Path(tempfile.gettempdir()) / "edisc-soak")
    args = ap.parse_args()
    import redis.asyncio as aioredis

    from edisc_core.logs import configure_logging
    from edisc_db.session import create_engine, session_factory
    from edisc_evidence.s3 import s3_client

    configure_logging()
    settings = Settings()
    projected = int(GROWTH_MARGIN * BYTES_PER_MESSAGE * args.messages) + WAL_HEADROOM_BYTES
    need = max(15 * 1024**3, projected + FLOOR_AFTER_RUN_BYTES)
    free = shutil.disk_usage(ROOT).free
    if free < need:
        print(
            f"refusing: {free / 1024**3:.1f} GB free; a {args.messages:,}-message soak is projected to use"
            f" {projected / 1024**3:.1f} GB (~{BYTES_PER_MESSAGE // 1000} KB/message x {GROWTH_MARGIN} + WAL)"
            f" and must leave {FLOOR_AFTER_RUN_BYTES / 1024**3:.0f} GB free: needs {need / 1024**3:.1f} GB",
            file=sys.stderr,
        )
        return 2
    per_unit = max(12, args.messages // (args.conversations * args.days))
    cfg = SoakConfig(
        conversations=args.conversations,
        days=args.days,
        messages_per_unit=per_unit,
        workers=args.workers,
        kills=args.kills,
        seed=args.seed,
        timeout_seconds=args.timeout,
        workdir=args.workdir,
    )
    engine = create_engine(settings, "app")
    redis = aioredis.from_url(settings.redis_url)
    temporal = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    try:
        async with s3_client(settings) as s3:
            result = await soak(
                cfg,
                sessions=session_factory(engine),
                s3=s3,
                settings=settings,
                temporal=temporal,
                limiter=RateLimiter(redis, settings.rate_limits),
            )
    finally:
        await redis.aclose()
        await engine.dispose()
    print(
        json.dumps(
            {
                "messages": cfg.messages,
                "workers": cfg.workers,
                "status": result.status,
                "seconds": round(result.seconds, 1),
                "messages_per_second": round(cfg.messages / result.seconds),
                "kills": result.kills,
                "problems": result.problems,
                "job_id": str(result.job_id),
            },
            indent=2,
        )
    )
    return 1 if result.problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
