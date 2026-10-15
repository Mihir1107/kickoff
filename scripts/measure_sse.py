"""Measure the job stream under many concurrent subscribers (M17 step 5, decision D4).

    MIN_FREE_GB=12 make test-env-up
    EDISC_ENV_FILE=.env.test uv run python scripts/measure_sse.py --streams 500 --jobs 50 --seconds 60

The REAL API app (ASGI, in this process) over the test stack's database, with production stream
settings (poll 1 s, re-check 15 s, queue 64). ``--streams`` subscribers spread over ``--jobs``
jobs; every ``--settle-every`` seconds each job's next unit is settled (row + chain event, as the
pipeline writes them) through a SEPARATE engine, so the API engine's statement count is the
stream's own load. Reports: API statements per second (polls, catch-ups, re-checks), latency from
a settle's commit to EACH subscriber receiving it (p50, p99, max), and memory (Python-allocated
bytes per stream, tracemalloc; and the process RSS).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import redis.asyncio as aioredis
from sqlalchemy import event
from temporalio.client import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.integration.api.conftest import Api, add_principal, make_api, new_api_tenant
from tests.integration.api.test_job_stream import new_job, settle, stream

from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.kms import LocalKmsClient
from edisc_core.logs import configure_logging
from edisc_core.settings import Settings
from edisc_db.session import create_engine, session_factory, warm_pool
from edisc_evidence.s3 import s3_client


async def run(streams: int, jobs: int, seconds: float, settle_every: float) -> dict[str, Any]:
    base = Settings()
    tmp = Path(tempfile.mkdtemp(prefix="edisc-sse-"))
    settings = base.model_copy(update={
        "api_dev_idp": True, "api_auth_failures_per_minute": 1_000_000, "local_kms_dir": tmp,
    })  # fmt: skip
    engine, writer = create_engine(settings, pool_size=40), create_engine(settings, pool_size=8)
    await warm_pool(engine)
    await warm_pool(writer)
    statements = {"n": 0}
    event.listen(engine.sync_engine, "before_cursor_execute",
                 lambda *a: statements.__setitem__("n", statements["n"] + 1))  # fmt: skip
    redis = aioredis.from_url(settings.redis_url)
    kms = LocalKmsClient(settings)
    temporal = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    try:
        async with s3_client(settings) as s3, make_api(
            settings, session_factory(engine), s3, temporal, RateLimiter(redis, settings.rate_limits), kms
        ) as api:  # fmt: skip
            return await _measure(api, session_factory(writer), statements, streams, jobs,
                                  seconds, settle_every)  # fmt: skip
    finally:
        await redis.aclose()
        await engine.dispose()
        await writer.dispose()


async def _measure(
    api: Api, writes: Any, statements: dict[str, int], streams: int, jobs: int, seconds: float,
    settle_every: float,
) -> dict[str, Any]:  # fmt: skip
    t = await new_api_tenant(api, cast(LocalKmsClient, api.resources.box.kms))
    units = int(seconds / settle_every) + 2
    created = [await new_job(api, t, units=units) for _ in range(jobs)]
    writer = cast(Api, SimpleNamespace(sessions=writes, settings=api.settings))
    api.client(t.subdomain)  # builds the app
    # one principal per 10 streams (the per-principal cap), each a tenant-wide reviewer
    tokens = []
    for _ in range(-(-streams // api.settings.api_stream_per_principal)):
        _, subject = await add_principal(api, t, roles=[("reviewer", "tenant", None)])
        tokens.append(t.token(api.settings, subject=subject))
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    per = api.settings.api_stream_per_principal
    clients = [stream(api, t, created[i % jobs], tokens[i // per]) for i in range(streams)]
    opened_at = time.monotonic()
    for start in range(
        0, streams, 50
    ):  # a ramp of 50 at a time, like tabs opening, not one instant
        batch = clients[start : start + 50]
        await asyncio.gather(*(c.__aenter__() for c in batch))
        await asyncio.gather(*(c.next(60) for c in batch))  # every first snapshot
    opened = time.monotonic() - opened_at
    await asyncio.sleep(2)
    held = tracemalloc.get_traced_memory()[0] - before
    settled_at: dict[str, float] = {}
    latencies: list[float] = []

    async def consume(c: Any) -> None:
        while True:
            e = await c.next(3600)
            now = time.monotonic()
            for u in e.data.get("units", []):
                latencies.append(now - settled_at[u["unit_key"] + str(c.path)])

    consumers = [asyncio.create_task(consume(c)) for c in clients]
    await asyncio.sleep(1)
    count0, t0 = statements["n"], time.monotonic()
    rounds = 0
    while time.monotonic() - t0 < seconds:
        for job in created:
            key = job.units[rounds]
            await settle(writer, t, job, key)
            stamp = time.monotonic()
            for c in clients:
                if c.path.endswith(f"/{job.id}/stream"):
                    settled_at[key + str(c.path)] = stamp
        rounds += 1
        await asyncio.sleep(max(0.0, settle_every - (time.monotonic() - t0) % settle_every))
    await asyncio.sleep(3)  # the last round's events
    elapsed = time.monotonic() - t0
    count = statements["n"] - count0
    peak = tracemalloc.get_traced_memory()[1] - before
    tracemalloc.stop()
    for task in consumers:
        task.cancel()
    await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)
    expected = rounds * streams
    q = (
        statistics.quantiles(latencies, n=100)
        if len(latencies) >= 100
        else [max(latencies or [0])] * 99
    )
    return {
        "streams": streams, "jobs": jobs, "seconds": round(elapsed, 1), "settle_every": settle_every,
        "open_all_s": round(opened, 2),
        "api_statements_per_s": round(count / elapsed, 1),
        "statements_per_stream_per_s": round(count / elapsed / streams, 3),
        "deliveries": len(latencies), "expected_deliveries": expected,
        "latency_ms": {"p50": round(statistics.median(latencies) * 1000, 1),
                       "p99": round(q[98] * 1000, 1), "max": round(max(latencies) * 1000, 1)},
        "python_bytes_per_stream_open": int(held / streams),
        "python_peak_bytes": peak,
        "rss_max_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20), 1),
    }  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--streams", type=int, default=500)
    parser.add_argument("--jobs", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--settle-every", type=float, default=5)
    a = parser.parse_args()
    configure_logging("WARNING")
    print(json.dumps(asyncio.run(run(a.streams, a.jobs, a.seconds, a.settle_every)), indent=2))


if __name__ == "__main__":
    main()
