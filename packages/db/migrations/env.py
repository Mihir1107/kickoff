"""Alembic environment: async engine, OWNER role, version table inside the app schema."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from edisc_core.settings import get_settings
from edisc_db.models import Base

config = context.config
settings = get_settings()
database = config.attributes.get("database") or settings.pg_db
config.attributes.setdefault("app_role", settings.pg_app_user)


def _url() -> str:
    return settings.pg_dsn("owner", db=database).replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        version_table_schema=settings.pg_schema,
        include_schemas=False,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    engine = create_async_engine(
        _url(), connect_args={"server_settings": {"search_path": settings.pg_schema}}
    )
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported; run against a live database")
asyncio.run(_main())
