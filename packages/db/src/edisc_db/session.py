"""Engines and the single way to touch tenant data: :func:`tenant_tx`.

``tenant_tx`` opens a transaction and sets ``app.tenant_id`` with ``set_config(..., is_local => true)``,
which is exactly ``SET LOCAL`` but parameterizable. The setting dies with the transaction, so a pooled
connection can never leak one tenant's context into another's work. Use it in API handlers and in
every Temporal activity.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from edisc_core.settings import Settings


def create_engine(
    settings: Settings,
    role: Literal["app", "owner", "superuser", "sweeper"] = "app",
    *,
    db: str | None = None,
    pool_size: int = 10,
) -> AsyncEngine:
    url = settings.pg_dsn(role, db=db).replace("postgresql://", "postgresql+asyncpg://", 1)
    server_settings = {"search_path": f"{settings.pg_schema},pg_temp"}
    if role in ("app", "sweeper"):
        # Never hang: a lock wait or an abandoned open transaction becomes a retryable error.
        # Migrations (owner) and tamper tests (superuser) are exempt: DDL may legitimately wait.
        server_settings["lock_timeout"] = str(settings.pg_lock_timeout_ms)
        server_settings["idle_in_transaction_session_timeout"] = str(
            settings.pg_idle_in_transaction_timeout_ms
        )
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={"server_settings": server_settings},
    )


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


def is_retryable_db_error(exc: BaseException) -> bool:
    """True for lock timeouts, deadlocks, serialization failures and terminated idle transactions.
    Callers (Temporal activities) retry the whole unit of work; nothing partial was committed."""
    if sqlstate_of(exc) in RETRYABLE_SQLSTATES:
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
