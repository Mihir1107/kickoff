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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from edisc_core.settings import Settings


def create_engine(
    settings: Settings,
    role: Literal["app", "owner", "superuser"] = "app",
    *,
    db: str | None = None,
    pool_size: int = 10,
) -> AsyncEngine:
    url = settings.pg_dsn(role, db=db).replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": f"{settings.pg_schema},pg_temp"}},
    )


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
