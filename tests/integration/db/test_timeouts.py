"""App/worker sessions never hang: lock waits and abandoned transactions become retryable errors."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_db.session import (
    create_engine,
    is_retryable_db_error,
    session_factory,
    sqlstate_of,
    tenant_tx,
)

from .conftest import Seeded


def _fast(settings: Settings, **kw: int) -> Settings:
    return settings.model_copy(
        update={"pg_lock_timeout_ms": 400, "pg_idle_in_transaction_timeout_ms": 60_000, **kw}
    )


async def test_timeouts_are_applied_to_app_sessions_only(settings: Settings) -> None:
    for role, expect_lock, expect_idle in (
        ("app", "30s", "1min"),
        ("sweeper", "30s", "1min"),
        ("owner", "0", "0"),
    ):
        engine = create_engine(settings, role)  # type: ignore[arg-type]
        try:
            async with engine.connect() as conn:
                assert (
                    await conn.execute(text("SHOW lock_timeout"))
                ).scalar_one() == expect_lock, role
                assert (
                    await conn.execute(text("SHOW idle_in_transaction_session_timeout"))
                ).scalar_one() == expect_idle
        finally:
            await engine.dispose()


async def test_forced_lock_wait_raises_a_retryable_error_instead_of_hanging(
    settings: Settings, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    engine = create_engine(_fast(settings), "app")
    sessions = session_factory(engine)
    try:
        async with tenant_tx(sessions, a.tenant_id) as holder:
            await holder.execute(
                text("SELECT 1 FROM collection_jobs WHERE id = :j FOR UPDATE"), {"j": a.job_id}
            )
            started = time.monotonic()
            with pytest.raises(DBAPIError) as exc:
                async with tenant_tx(sessions, a.tenant_id) as waiter:
                    await waiter.execute(
                        text("UPDATE collection_jobs SET status = 'running' WHERE id = :j"),
                        {"j": a.job_id},
                    )
            elapsed = time.monotonic() - started
        assert sqlstate_of(exc.value) == "55P03"  # lock_not_available
        assert is_retryable_db_error(exc.value)
        assert 0.3 < elapsed < 5, f"waited {elapsed:.2f}s"
    finally:
        await engine.dispose()


async def test_application_level_deadlock_shape_surfaces_as_retryable_error(
    settings: Settings, two_tenants: tuple[Seeded, Seeded]
) -> None:
    """The M7.1 bug shape: session A holds FOR UPDATE on a connection row and awaits session B, whose
    FK insert needs KEY SHARE on that row. Postgres sees no deadlock; lock_timeout must break it."""
    a, _ = two_tenants
    engine = create_engine(_fast(settings), "app")
    sessions = session_factory(engine)
    try:
        async with tenant_tx(sessions, a.tenant_id) as holder:
            await holder.execute(
                text("SELECT 1 FROM connections WHERE id = :c FOR UPDATE"), {"c": a.connection_id}
            )
            with pytest.raises(DBAPIError) as exc:
                async with tenant_tx(
                    sessions, a.tenant_id
                ) as inner:  # awaited while A still holds the lock
                    await inner.execute(
                        text(
                            "INSERT INTO token_refresh_journal (id, tenant_id, connection_id, based_on_version,"
                            " encrypted_access_token, token_key_id, token_key_version) VALUES (:i, :t, :c, 0, 'x', 'k', '1')"
                        ),
                        {"i": new_id(), "t": a.tenant_id, "c": a.connection_id},
                    )
        assert is_retryable_db_error(exc.value)
    finally:
        await engine.dispose()


async def test_abandoned_open_transaction_is_terminated(
    settings: Settings, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    engine = create_engine(_fast(settings, pg_idle_in_transaction_timeout_ms=300), "app")
    sessions = session_factory(engine)
    try:

        async def stuck_caller() -> None:
            async with tenant_tx(sessions, a.tenant_id) as s:
                await s.execute(
                    text("SELECT 1 FROM collection_jobs WHERE id = :j FOR UPDATE"), {"j": a.job_id}
                )
                await asyncio.sleep(1.0)  # holds a lock, does nothing
                await s.execute(text("SELECT 1"))

        with pytest.raises(DBAPIError) as exc:
            await stuck_caller()
        assert is_retryable_db_error(exc.value)
        # its lock was released: another session proceeds immediately
        async with tenant_tx(sessions, a.tenant_id) as s:
            await s.execute(
                text("SELECT 1 FROM collection_jobs WHERE id = :j FOR UPDATE"), {"j": a.job_id}
            )
    finally:
        await engine.dispose()
