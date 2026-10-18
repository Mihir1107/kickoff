"""Subprocess of the directory SIGKILL test: captures one connection's directory with the product
code (``edisc_worker.directory.refresh_one``) and blocks at a crash point until it is killed.

python -m tests.integration.api.directory_kill_target <tenant_id> <connection_id> <point> <dir>
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid

import redis.asyncio as aioredis

from edisc_connector_dummy.connector import DummyConnector
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.settings import Settings
from edisc_db.session import create_engine, session_factory
from edisc_worker import directory
from edisc_worker.renders import BarrierHooks


async def main() -> None:
    tenant, connection, point, where = (
        uuid.UUID(sys.argv[1]), uuid.UUID(sys.argv[2]), sys.argv[3], sys.argv[4],
    )  # fmt: skip
    settings = Settings()
    engine = create_engine(settings)
    redis = aioredis.from_url(settings.redis_url)
    try:
        await directory.refresh_one(
            session_factory(engine), DummyConnector(RateLimiter(redis, settings.rate_limits)),
            tenant_id=tenant, connection_id=connection, deadline=time.monotonic() + 600,
            hooks=BarrierHooks(f"{point}:{where}"),
        )  # fmt: skip
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
