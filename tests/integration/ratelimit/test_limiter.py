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
