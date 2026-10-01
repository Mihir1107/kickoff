from __future__ import annotations

import asyncio
import uuid

import pytest
import redis.asyncio as aioredis

from edisc_connectors_base.ratelimit import BucketKey, RateLimiter, RateLimitNotConfiguredError
from edisc_core.settings import RateLimitConfig, Settings


async def test_unconfigured_method_is_refused_not_unlimited(settings: Settings) -> None:
    client = aioredis.from_url(settings.redis_url)
    try:
        limiter = RateLimiter(client, {"dummy.fetch": RateLimitConfig(rate_per_second=5, burst=1)})
        with pytest.raises(RateLimitNotConfiguredError, match="refusing to run unlimited"):
            await limiter.acquire(BucketKey(uuid.uuid4(), "dummy", "W", "users.list"))
    finally:
        await client.aclose()


async def test_limits_come_from_settings(settings: Settings) -> None:
    assert settings.rate_limits["dummy.fetch"].rate_per_second > 0
    assert settings.rate_limits["dummy.fetch"].burst >= 1


async def test_buckets_are_separate_per_tenant_workspace_and_method(settings: Settings) -> None:
    client = aioredis.from_url(settings.redis_url)
    try:
        limiter = RateLimiter(
            client,
            {
                "dummy.fetch": RateLimitConfig(rate_per_second=1, burst=1),
                "dummy.other": RateLimitConfig(rate_per_second=1, burst=1),
            },
        )
        base = BucketKey(uuid.uuid4(), "dummy", "W1", "fetch")
        keys = {
            base.redis_keys()[0],
            BucketKey(uuid.uuid4(), "dummy", "W1", "fetch").redis_keys()[0],
            BucketKey(base.tenant_id, "dummy", "W2", "fetch").redis_keys()[0],
            BucketKey(base.tenant_id, "dummy", "W1", "other").redis_keys()[0],
        }
        assert len(keys) == 4
        assert all(k.startswith("rl:{") for k in keys)  # cluster hash tag
        # behaviourally independent: 4 buckets at 1 req/s (starting empty) grant in ~1 s total, not ~4 s
        distinct = [
            base,
            BucketKey(uuid.uuid4(), "dummy", "W1", "fetch"),
            BucketKey(base.tenant_id, "dummy", "W2", "fetch"),
            BucketKey(base.tenant_id, "dummy", "W1", "other"),
        ]
        grants = await asyncio.gather(*(limiter.acquire(k) for k in distinct))
        assert max(g.waited_seconds for g in grants) < 1.6
    finally:
        await client.aclose()


async def test_long_waits_call_on_wait_in_bounded_chunks(settings: Settings) -> None:
    """A long Retry-After must not be one silent sleep: on_wait (activity heartbeat, cancel and
    time-box checks) runs at least every ``wait_chunk_seconds``."""
    client = aioredis.from_url(settings.redis_url)
    try:
        limiter = RateLimiter(
            client,
            {"dummy.fetch": RateLimitConfig(rate_per_second=100, burst=1)},
            wait_chunk_seconds=0.2,
        )
        key = BucketKey(uuid.uuid4(), "dummy", "W1", "fetch")
        await limiter.pause(key, 1.5)
        calls: list[float] = []

        async def on_wait(_reason: str, seconds: float) -> None:
            calls.append(seconds)

        loop = asyncio.get_running_loop()
        started = loop.time()
        await limiter.acquire(key, on_wait=on_wait)
        assert loop.time() - started >= 1.2
        assert len(calls) >= 6 and max(calls) <= 0.2, calls
    finally:
        await client.aclose()
