"""One rate-limited worker PROCESS for the multi-process tests. Writes JSON lines to --out.

    python -m tests.integration.ratelimit.worker --tenant T --wid 3 --rate 40 --burst 4 --out f \\
        [--duration S | --units N] [--skew SECONDS] [--pause-at K --retry-after S]

Every request goes through ``call_with_limits`` exactly as a connector would. Each grant is logged with
the Redis SERVER time (the only clock the limiter uses).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import IO

import redis.asyncio as aioredis

from edisc_connectors_base.ratelimit import (
    BucketKey,
    Grant,
    RateLimiter,
    SourceThrottledError,
    WaitCallback,
    call_with_limits,
)
from edisc_core.settings import RateLimitConfig, Settings


class RecordingLimiter(RateLimiter):
    def __init__(self, *args: object, out: IO[str], wid: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.out, self.wid, self.last_grant = out, wid, 0

    def emit(self, **record: object) -> None:
        self.out.write(json.dumps({"w": self.wid, **record}) + "\n")

    async def acquire(self, key: BucketKey, *, on_wait: WaitCallback | None = None) -> Grant:
        grant = await super().acquire(key, on_wait=on_wait)
        self.last_grant = grant.server_time_us
        self.emit(event="grant", t=grant.server_time_us)
        return grant

    async def pause(self, key: BucketKey, retry_after_seconds: float) -> int | None:
        until = await super().pause(key, retry_after_seconds)
        self.emit(event="pause", **{"from": self.last_grant, "until": until})
        return until


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--wid", type=int, required=True)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--burst", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--units", type=int)
    ap.add_argument("--skew", type=float, default=0.0)
    ap.add_argument("--pause-at", type=int)
    ap.add_argument("--retry-after", type=float, default=1.0)
    args = ap.parse_args()
    if args.skew:  # this worker's wall clock is wrong; the limiter must not care
        real_time = time.time
        time.time = lambda: real_time() + args.skew  # type: ignore[assignment]
    asyncio.run(run(args))


async def run(args: argparse.Namespace) -> None:
    settings = Settings()
    client = aioredis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
    with open(args.out, "w", buffering=1) as out:  # noqa: ASYNC230 - line-buffered log read by the parent
        limiter = RecordingLimiter(
            client,
            {"dummy.fetch": RateLimitConfig(rate_per_second=args.rate, burst=args.burst)},
            backoff_initial_seconds=0.1,
            backoff_max_seconds=0.5,
            out=out,
            wid=args.wid,
        )
        key = BucketKey(uuid.UUID(args.tenant), "dummy", "W1", "fetch")
        requests = 0

        async def on_wait(reason: str, _seconds: float) -> None:
            if reason == "limiter_unavailable":
                limiter.emit(event="unavailable")

        start = time.monotonic()
        unit = 0
        while (args.units is None or unit < args.units) and (
            args.duration is None or time.monotonic() - start < args.duration
        ):

            async def request() -> None:
                nonlocal requests
                requests += 1
                if args.pause_at is not None and requests == args.pause_at:
                    raise SourceThrottledError(args.retry_after)  # the source answered 429

            await call_with_limits(limiter, key, request, on_wait=on_wait)
            limiter.emit(event="done", unit=unit)
            unit += 1
    await client.aclose()


if __name__ == "__main__":
    main()
