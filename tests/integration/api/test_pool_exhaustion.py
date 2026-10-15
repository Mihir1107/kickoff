"""A request beyond the database pool's capacity waits a bounded time, then fails loudly.

Pools are fixed (ADR 0015 §24.13): no overflow connection is opened for a burst. A request that
finds every connection busy waits `pg_pool_timeout_ms` for one, then gets 503 `db_pool_exhausted`
(with `Retry-After`) and an error log line, never a hang and never a 500. The worker side (a
transient, retryable activity error) is `tests/integration/db/test_pool.py`."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import AsyncIterator

import pytest
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

from edisc_api.app import redis_client
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.kms import LocalKmsClient
from edisc_db.session import create_engine, session_factory, warm_pool
from tests.logcapture import captured_logs

from .conftest import Api, TenantCtx, make_api

BOUND_MS = 300


@pytest.fixture
async def tight(
    api: Api, s3: S3Client, temporal: Client, limiter: RateLimiter, kms: LocalKmsClient
) -> AsyncIterator[tuple[Api, object]]:
    """The API over a warmed pool of ONE connection that waits at most BOUND_MS for it."""
    settings = api.settings.model_copy(update={"pg_pool_timeout_ms": BOUND_MS})
    engine = create_engine(settings, pool_size=1)
    try:
        await warm_pool(engine)
        async with make_api(settings, session_factory(engine), s3, temporal, limiter, kms) as made:
            yield made, engine
    finally:
        await engine.dispose()


async def test_a_request_beyond_pool_capacity_gets_503_within_the_bound(
    tight: tuple[Api, object], tenant: TenantCtx
) -> None:
    small, engine = tight
    async with small.client(tenant.subdomain, tenant.token(small.settings)) as c:
        assert (await c.get("/v1/me/permissions")).status_code == 200  # the pool works
        with captured_logs() as logs:
            async with engine.connect() as held:  # type: ignore[attr-defined]
                await held.exec_driver_sql("SELECT 1")
                started = time.monotonic()
                r = await c.get("/v1/me/permissions")
                waited = time.monotonic() - started
            logs.require("db pool exhausted", timeout_ms=BOUND_MS)
        assert r.status_code == 503, r.text
        assert r.json() == {
            "error": "db_pool_exhausted",
            "detail": "the service is busy; retry shortly",
        }
        assert r.headers["retry-after"] == "1"
        assert BOUND_MS / 1000 <= waited < BOUND_MS / 1000 + 2  # waited the bound, then failed
        # the connection is back: the next request is served
        assert (await c.get("/v1/me/permissions")).status_code == 200


async def test_a_request_beyond_the_redis_pool_gets_503_within_the_bound(
    api: Api, tenant: TenantCtx
) -> None:
    """The API's Redis pool is fixed and blocking (the authentication throttle runs on every
    authenticated request): a burst beyond it waits, then answers 503 service_busy. redis-py's
    default pool answered MaxConnectionsError (an unhandled 500) in the 500-stream measurement."""
    settings = api.settings.model_copy(
        update={"redis_max_connections": 1, "redis_pool_timeout_seconds": BOUND_MS / 1000}
    )
    redis = redis_client(settings)
    small = Api(settings, api.sessions, api.s3, api.temporal,
                dataclasses.replace(api.resources, settings=settings, redis=redis,
                                    keyring=api.resources.keys,
                                    client_signer=api.resources.signer), api.http)  # fmt: skip
    try:
        async with small.client(tenant.subdomain, tenant.token(settings)) as c:
            assert (await c.get("/v1/me")).status_code == 200
            held = await redis.connection_pool.get_connection()  # the only connection
            try:
                started = time.monotonic()
                r = await c.get("/v1/me")
                waited = time.monotonic() - started
            finally:
                await redis.connection_pool.release(held)
            assert r.status_code == 503 and r.json()["error"] == "service_busy"
            assert r.headers["retry-after"] == "1"
            assert BOUND_MS / 1000 <= waited < BOUND_MS / 1000 + 2
            assert (await c.get("/v1/me")).status_code == 200  # the connection is back
    finally:
        await redis.aclose()
