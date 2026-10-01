"""Distributed rate limiting shared by every worker (Redis token buckets).

- **Bucket** = tenant + source + workspace/org + API method. Limits come from configuration per
  ``source.method``; an unconfigured method raises, never runs unlimited.
- **Atomic check-and-take** in one Lua script. Time comes from Redis ``TIME`` only: worker clocks
  are never consulted, so clock skew between workers cannot change the rate.
- **New or lost buckets start EMPTY.** After a Redis restart (state lost), there is no burst:
  tokens refill at the configured rate.
- **Source back-pressure is shared:** a 429 / Retry-After seen by one worker pauses the whole bucket
  for every worker, whatever our bucket believed, and drains its tokens.
- **Fail closed:** if Redis is unavailable, ``acquire`` keeps waiting (with backoff) and never returns
  without a grant. Nothing is fetched unthrottled and no work unit is skipped: the caller simply
  waits.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import redis.asyncio as aioredis
import redis.exceptions

from edisc_core.logs import get_logger
from edisc_core.settings import RateLimitConfig

log = get_logger(__name__)

# KEYS[1] bucket hash, KEYS[2] pause key. ARGV[1] rate/s, ARGV[2] burst, ARGV[3] ttl seconds.
# Returns {granted (0|1), wait_us, now_us}. Integer-only returns (Lua floats would be truncated).
_TAKE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000000 + tonumber(t[2])
local paused_until = tonumber(redis.call('GET', KEYS[2]) or '0')
if now < paused_until then
  return {0, paused_until - now, now}
end
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = 0
  ts = now
end
if now > ts then
  tokens = math.min(burst, tokens + (now - ts) * rate / 1000000)
end
local granted = 0
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
  granted = 1
else
  wait = math.ceil((1 - tokens) * 1000000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', string.format('%.17g', tokens), 'ts', string.format('%d', math.max(now, ts)))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return {granted, wait, now}
"""

# KEYS[1] bucket hash, KEYS[2] pause key. ARGV[1] pause microseconds, ARGV[2] ttl seconds.
# Extends (never shortens) the pause and drains the bucket. Returns {paused_until_us, now_us}.
_PAUSE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000000 + tonumber(t[2])
local target = now + tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[2]) or '0')
if target > current then
  redis.call('SET', KEYS[2], string.format('%d', target), 'PX', math.ceil((target - now) / 1000) + 1000)
else
  target = current
end
redis.call('HSET', KEYS[1], 'tokens', '0', 'ts', string.format('%d', now))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return {target, now}
"""

_UNAVAILABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, OSError)


class RateLimitNotConfiguredError(KeyError):
    pass


@dataclass(frozen=True)
class BucketKey:
    tenant_id: uuid.UUID
    source: str
    workspace: str
    method: str

    @property
    def config_key(self) -> str:
        return f"{self.source}.{self.method}"

    def redis_keys(self) -> tuple[str, str]:
        # One hash tag {...} so both keys live in the same slot (Redis Cluster-safe scripts).
        parts = ":".join(
            quote(p, safe="")
            for p in (str(self.tenant_id), self.source, self.workspace, self.method)
        )
        return f"rl:{{{parts}}}:bucket", f"rl:{{{parts}}}:pause"


@dataclass(frozen=True)
class Grant:
    server_time_us: int  # Redis TIME at the moment the token was taken
    waited_seconds: float


WaitCallback = Callable[[str, float], Awaitable[None]]

# Set by the activity around connector calls: connectors call ``call_with_limits`` without knowing about
# Temporal, and every limiter wait still heartbeats / checks the time box and the job's cancel flag.
current_wait_callback: contextvars.ContextVar[WaitCallback | None] = contextvars.ContextVar(
    "edisc_current_wait_callback", default=None
)
"""(reason, seconds) before each wait: "throttled", "paused" or "limiter_unavailable". Use it to
heartbeat Temporal activities during long waits."""


class RateLimiter:
    def __init__(
        self,
        client: aioredis.Redis,
        limits: Mapping[str, RateLimitConfig],
        *,
        backoff_initial_seconds: float = 0.2,
        backoff_max_seconds: float = 10.0,
        bucket_ttl_seconds: int = 3600,
    ) -> None:
        self._client = client
        self._limits = dict(limits)
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._ttl = bucket_ttl_seconds
        self._take = client.register_script(_TAKE)
        self._pause = client.register_script(_PAUSE)

    def config(self, key: BucketKey) -> RateLimitConfig:
        try:
            return self._limits[key.config_key]
        except KeyError:
            raise RateLimitNotConfiguredError(
                f"no rate limit configured for {key.config_key!r} (EDISC_RATE_LIMITS); refusing to run unlimited"
            ) from None

    async def acquire(self, key: BucketKey, *, on_wait: WaitCallback | None = None) -> Grant:
        """Block until a token is granted. Never returns without one (fail closed)."""
        cfg = self.config(key)
        bucket, pause = key.redis_keys()
        waited, backoff, outage = 0.0, self._backoff_initial, 0
        while True:
            try:
                granted, wait_us, now_us = await self._take(
                    keys=[bucket, pause], args=[cfg.rate_per_second, cfg.burst, self._ttl]
                )
            except _UNAVAILABLE as exc:
                outage += 1
                if outage == 1 or outage % 10 == 0:
                    log.warning(
                        "rate limiter unavailable; pausing (fail closed)",
                        bucket=key.config_key,
                        attempt=outage,
                        error=type(exc).__name__,
                    )
                if on_wait is not None:
                    await on_wait("limiter_unavailable", backoff)
                await asyncio.sleep(backoff)
                waited += backoff
                backoff = min(backoff * 2, self._backoff_max)
                continue
            if outage:
                log.info("rate limiter available again", bucket=key.config_key, attempts=outage)
                outage, backoff = 0, self._backoff_initial
            if granted:
                return Grant(int(now_us), waited)
            delay = int(wait_us) / 1_000_000
            if on_wait is not None:
                await on_wait("throttled", delay)
            await asyncio.sleep(delay)
            waited += delay

    async def pause(self, key: BucketKey, retry_after_seconds: float) -> int | None:
        """Pause the bucket for EVERY worker (source said 429 / Retry-After). Returns the server-time end
        of the pause (µs). If Redis is unavailable, keeps trying to publish the pause until the pause
        itself would have ended; meanwhile no worker can acquire anyway (fail closed)."""
        if retry_after_seconds <= 0:
            retry_after_seconds = 1.0
        bucket, pause = key.redis_keys()
        deadline = (
            time.monotonic() + retry_after_seconds
        )  # a local DURATION, never compared across workers
        backoff = self._backoff_initial
        while True:
            try:
                until_us, _ = await self._pause(
                    keys=[bucket, pause], args=[int(retry_after_seconds * 1_000_000), self._ttl]
                )
            except _UNAVAILABLE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                await asyncio.sleep(min(backoff, remaining))
                backoff = min(backoff * 2, self._backoff_max)
                continue
            log.warning(
                "source throttled us; bucket paused for all workers",
                bucket=key.config_key,
                seconds=retry_after_seconds,
            )
            return int(until_us)


class _LimiterLike(Protocol):
    async def acquire(self, key: BucketKey, *, on_wait: WaitCallback | None = None) -> Grant: ...

    async def pause(self, key: BucketKey, retry_after_seconds: float) -> int | None: ...


class SourceThrottledError(Exception):
    """Raised by connectors when the source answers 429 / Retry-After (or an equivalent signal)."""

    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__(f"source throttled; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


async def call_with_limits[T](
    limiter: _LimiterLike,
    key: BucketKey,
    request: Callable[[], Awaitable[T]],
    *,
    on_wait: WaitCallback | None = None,
    default_retry_after_seconds: float = 5.0,
) -> T:
    """The connector rate-limit hook: take a token before EVERY request; on a source 429, pause the
    bucket for all workers and retry the same request after the pause (nothing is skipped)."""
    callback = on_wait or current_wait_callback.get()
    while True:
        await limiter.acquire(key, on_wait=callback)
        try:
            return await request()
        except SourceThrottledError as exc:
            await limiter.pause(key, exc.retry_after_seconds or default_retry_after_seconds)
