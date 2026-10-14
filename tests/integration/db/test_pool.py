"""Fixed, warmed database pools (ADR 0015 §24.13).

CI run 38536427956 failed `test_concurrent_writers_produce_one_anchor_per_due_point` with a 466 ms
loop stall: 200 writers on a pool of 40 + 40 overflow opened forty new connections per burst (and
closed them again), each running asyncpg's SCRAM handshake (PBKDF2 in Python) on the event loop.
Pools are now fixed and warmed at startup; any connection a warmed pool opens for a new slot is
churn, reported through `loopblock.report_product` so the test that caused it fails."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import event, text

from edisc_core import loopblock
from edisc_core.settings import Settings
from edisc_db.session import create_engine, pool_churn, session_factory, tenant_tx, warm_pool


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
