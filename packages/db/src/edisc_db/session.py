"""Engines and the single way to touch tenant data: :func:`tenant_tx`.

``tenant_tx`` opens a transaction and sets ``app.tenant_id`` with ``set_config(..., is_local => true)``,
which is exactly ``SET LOCAL`` but parameterizable. The setting dies with the transaction, so a pooled
connection can never leak one tenant's context into another's work. Use it in API handlers and in
every Temporal activity.
"""

from __future__ import annotations

import uuid
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

from sqlalchemy import Engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry, Pool

from edisc_core import loopblock
from edisc_core.logs import get_logger
from edisc_core.settings import Settings

log = get_logger(__name__)

# connections a WARMED pool opened for a new pool slot (process-wide; tests fail on any: ADR 0015
# §24.13). A reconnect of an invalidated slot (pre-ping, a terminated session) is not counted.
_churn = 0
_warmed: weakref.WeakSet[Pool] = weakref.WeakSet()


def pool_churn() -> int:
    """How many connections warmed pools of this process opened for a new slot so far."""
    return _churn


def _count_churn(engine: Engine) -> Callable[[object, ConnectionPoolEntry], None]:
    ref = weakref.ref(engine)

    def on_connect(dbapi_conn: object, record: ConnectionPoolEntry) -> None:
        global _churn
        info = record.record_info  # survives invalidation (.info does not); None only when detached
        if info is None:
            return
        first, info["edisc_connected"] = not info.get("edisc_connected"), True
        owner = ref()
        if first and owner is not None and owner.pool in _warmed:
            _churn += 1
            log.warning("db pool opened a connection after warm-up")
            loopblock.report_product(
                "database pool churn",
                "a warmed pool opened a connection for a new slot (asyncpg's SCRAM handshake runs"
                " on the event loop; pools are fixed and warmed at startup: ADR 0015 §24.13)",
            )

    return on_connect


def create_engine(
    settings: Settings,
    role: Literal["app", "owner", "superuser", "sweeper"] = "app",
    *,
    db: str | None = None,
    pool_size: int = 20,
) -> AsyncEngine:
    """A FIXED pool of ``pool_size`` connections (no overflow): a new asyncpg connection runs the
    SCRAM handshake (4,096 PBKDF2 iterations in Python, ~4 ms of CPU here, more on CI runners) ON
    THE EVENT LOOP, so overflow connections opened and closed per burst stalled the loop by
    hundreds of milliseconds (ADR 0015 §24.13). Call :func:`warm_pool` at startup. Waiters queue
    for a free connection instead, for at most ``pg_pool_timeout_ms``: then the checkout raises
    (:func:`is_pool_exhausted`), never waits for ever."""
    url = settings.pg_dsn(role, db=db).replace("postgresql://", "postgresql+asyncpg://", 1)
    server_settings = {"search_path": f"{settings.pg_schema},pg_temp"}
    if role in ("app", "sweeper"):
        # Never hang: a lock wait or an abandoned open transaction becomes a retryable error.
        # Migrations (owner) and tamper tests (superuser) are exempt: DDL may legitimately wait.
        server_settings["lock_timeout"] = str(settings.pg_lock_timeout_ms)
        server_settings["idle_in_transaction_session_timeout"] = str(
            settings.pg_idle_in_transaction_timeout_ms
        )
    engine = create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=settings.pg_pool_timeout_ms / 1000,
        pool_pre_ping=True,
        # a statement error's text never carries its parameters: it reaches logs, unit errors in the
        # chain and API answers (ADR 0016 amendment, D11); the SQLSTATE stays on the error
        hide_parameters=True,
        connect_args={"server_settings": server_settings},
    )
    event.listen(engine.sync_engine, "connect", _count_churn(engine.sync_engine))
    return engine


async def warm_pool(engine: AsyncEngine) -> None:
    """Open every connection of the pool now, ONE AT A TIME (each handshake is its own short turn
    of the loop, never a burst), and mark the pool warmed: any later connection for a new slot is
    churn (:func:`pool_churn`)."""
    pool = engine.sync_engine.pool
    held = []
    try:
        for _ in range(pool.size()):  # type: ignore[attr-defined]
            held.append(await engine.connect())
            await held[-1].exec_driver_sql("SELECT 1")
    finally:
        for conn in held:
            await conn.close()
    _warmed.add(pool)


# SQLSTATEs that mean "try the whole transaction again": nothing was committed.
RETRYABLE_SQLSTATES = frozenset(
    {
        "55P03",  # lock_not_available (lock_timeout)
        "40P01",  # deadlock_detected
        "40001",  # serialization_failure
        "25P03",  # idle_in_transaction_session_timeout
        "57P01",  # admin_shutdown (connection terminated)
    }
)


def sqlstate_of(exc: BaseException) -> str | None:
    """SQLSTATE of a SQLAlchemy/asyncpg error, following the wrapper chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if isinstance(code, str):
            return code
        current = getattr(current, "orig", None) or current.__cause__
    return None


def is_pool_exhausted(exc: BaseException) -> bool:
    """True when no connection of the fixed pool became free within ``pg_pool_timeout_ms``. Nothing
    ran on the database: the whole unit of work may be retried. The API answers 503
    ``db_pool_exhausted``; activities classify it as transient."""
    return isinstance(exc, PoolTimeoutError)


def is_retryable_db_error(exc: BaseException) -> bool:
    """True for lock timeouts, deadlocks, serialization failures, terminated idle transactions and
    an exhausted pool. Callers (Temporal activities) retry the whole unit of work; nothing partial
    was committed."""
    if sqlstate_of(exc) in RETRYABLE_SQLSTATES or is_pool_exhausted(exc):
        return True
    # a session killed while idle in transaction surfaces as a dropped connection on next use
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_tx(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """Transaction scoped to one tenant. Commits on success, rolls back on any exception."""
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_id must be a UUID")
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield session


async def create_tenant(
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    subdomain: str,
    kms_key_ref: str,
) -> uuid.UUID:
    """Create a tenant via the narrow SECURITY DEFINER function (the app role cannot INSERT directly)."""
    async with sessions() as session, session.begin():
        result = await session.execute(
            text("SELECT create_tenant(:id, :name, :subdomain, :kms)"),
            {"id": tenant_id, "name": name, "subdomain": subdomain, "kms": kms_key_ref},
        )
        created: uuid.UUID = result.scalar_one()
        return created
