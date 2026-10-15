"""Fixed, warmed database pools (ADR 0015 §24.13).

CI run 38536427956 failed `test_concurrent_writers_produce_one_anchor_per_due_point` with a 466 ms
loop stall: 200 writers on a pool of 40 + 40 overflow opened forty new connections per burst (and
closed them again), each running asyncpg's SCRAM handshake (PBKDF2 in Python) on the event loop.
Pools are now fixed and warmed at startup; any connection a warmed pool opens for a new slot is
churn, reported through `loopblock.report_product` so the test that caused it fails."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core import loopblock
from edisc_core.settings import Settings
from edisc_db.session import (
    create_engine,
    is_pool_exhausted,
    is_retryable_db_error,
    pool_churn,
    session_factory,
    tenant_tx,
    warm_pool,
)
from edisc_worker.activities import Activities
from edisc_worker.contracts import ErrorClass, JobRef, OverviewRequest


def _connects(engine: Any) -> list[int]:
    seen: list[int] = []
    event.listen(engine.sync_engine, "connect", lambda *a: seen.append(1))
    return seen


async def test_a_burst_on_a_warmed_pool_opens_no_connection(settings: Settings) -> None:
    engine = create_engine(settings, "app", pool_size=8)
    try:
        await warm_pool(engine)
        connects, churn = _connects(engine), pool_churn()
        sessions = session_factory(engine)

        async def writer() -> None:
            for _ in range(2):  # the anchor test's shape: append, then anchor_if_due's own tx
                async with tenant_tx(sessions, uuid.uuid4()) as s:
                    await s.execute(text("SELECT 1"))

        await asyncio.gather(*(writer() for _ in range(200)))
        assert connects == [] and pool_churn() == churn
        assert engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


async def test_the_production_pool_is_fixed(settings: Settings) -> None:
    engine = create_engine(settings, "app")
    try:
        pool: Any = engine.sync_engine.pool
        assert pool.size() == 20 and pool._max_overflow == 0
        assert pool._timeout == settings.pg_pool_timeout_ms / 1000 == 10  # bounded wait
    finally:
        await engine.dispose()


async def test_a_reconnect_of_an_invalidated_connection_is_not_churn(settings: Settings) -> None:
    engine = create_engine(settings, "app", pool_size=1)  # the next checkout reuses the slot
    try:
        await warm_pool(engine)
        connects, churn = _connects(engine), pool_churn()
        async with engine.connect() as conn:
            await conn.invalidate()  # a terminated session: pre-ping replaces it in its slot
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        assert connects == [1] and pool_churn() == churn
    finally:
        await engine.dispose()


async def test_a_new_slot_after_warm_up_is_churn_and_is_reported(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    got: list[loopblock.Violation] = []
    engine = create_engine(settings, "app", pool_size=2)
    try:
        pool: Any = engine.sync_engine.pool
        monkeypatch.setattr(pool, "size", lambda: 1)  # warm one slot of two
        await warm_pool(engine)
        monkeypatch.undo()
        churn = pool_churn()
        with loopblock.installed(got.append):
            async with engine.connect() as a, engine.connect() as b:  # the second slot: new
                await a.execute(text("SELECT 1"))
                await b.execute(text("SELECT 1"))
        assert pool_churn() == churn + 1
        assert [v.kind for v in got] == ["database pool churn"] and got[0].origin == "product"
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ an exhausted pool fails loudly
# A fixed pool has no overflow: a caller beyond its capacity waits for a free connection, but only
# for pg_pool_timeout_ms; then it fails with a retryable error, never hangs. The API's answer (503
# db_pool_exhausted) is tested in tests/integration/api/test_pool_exhaustion.py.
BOUND_MS = 300


async def test_a_checkout_beyond_capacity_fails_within_the_bound(settings: Settings) -> None:
    engine = create_engine(
        settings.model_copy(update={"pg_pool_timeout_ms": BOUND_MS}), pool_size=1
    )
    try:
        await warm_pool(engine)
        sessions = session_factory(engine)
        async with engine.connect() as held:
            await held.execute(text("SELECT 1"))
            started = time.monotonic()
            with pytest.raises(PoolTimeoutError) as exc:
                async with tenant_tx(sessions, uuid.uuid4()) as s:
                    await s.execute(text("SELECT 1"))
            waited = time.monotonic() - started
        assert is_pool_exhausted(exc.value) and is_retryable_db_error(exc.value)
        assert BOUND_MS / 1000 <= waited < BOUND_MS / 1000 + 2  # waited the bound, then failed
        async with tenant_tx(sessions, uuid.uuid4()) as s:  # the slot is free again: it recovers
            assert (await s.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_an_exhausted_pool_fails_an_activity_retryably(
    settings: Settings, s3: S3Client, limiter: RateLimiter
) -> None:
    tight = settings.model_copy(update={"pg_pool_timeout_ms": BOUND_MS})
    engine = create_engine(tight, pool_size=1)
    try:
        await warm_pool(engine)
        acts = Activities(session_factory(engine), s3, tight, {"dummy": DummyConnector(limiter)})
        ref = JobRef(str(uuid.uuid4()), str(uuid.uuid4()))
        async with engine.connect() as held:
            await held.execute(text("SELECT 1"))
            started = time.monotonic()
            with pytest.raises(ApplicationError) as exc:
                await ActivityEnvironment().run(acts.job_overview, OverviewRequest(ref, 10))
            waited = time.monotonic() - started
        # the workflow retries it with backoff (ADR 0012 §3), it does not fail the unit
        assert exc.value.type == ErrorClass.TRANSIENT.value and not exc.value.non_retryable
        assert BOUND_MS / 1000 <= waited < BOUND_MS / 1000 + 2
    finally:
        await engine.dispose()
