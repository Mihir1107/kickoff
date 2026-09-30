"""Migrations: clean upgrade, downgrade, re-upgrade on a scratch DB; models match the migrated schema."""

from __future__ import annotations

import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection

from edisc_core.settings import Settings
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import downgrade, upgrade
from edisc_db.models import Base
from edisc_db.session import create_engine

from .conftest import Connect

SCRATCH_DB = "edisc_migtest"


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(
        conn, opts={"compare_type": True, "version_table_schema": "edisc"}
    )
    return [d for d in compare_metadata(ctx, Base.metadata) if "alembic_version" not in repr(d)]


async def _schema_diff(settings: Settings, db: str) -> list[object]:
    engine = create_engine(settings, "owner", db=db)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_diff)
    finally:
        await engine.dispose()


async def test_roundtrip_and_no_model_drift(connect: Connect, settings: Settings) -> None:
    su = await connect("superuser", db="postgres")
    try:
        await su.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    finally:
        await su.close()

    await bootstrap(settings, db=SCRATCH_DB)
    await asyncio.to_thread(upgrade, SCRATCH_DB)
    assert await _schema_diff(settings, SCRATCH_DB) == []

    await asyncio.to_thread(lambda: downgrade(SCRATCH_DB, revision="base"))
    owner = await connect("owner", db=SCRATCH_DB)
    try:
        leftover = await owner.fetchval(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'edisc' AND c.relname <> 'alembic_version' AND c.relkind IN ('r', 'v')"
        )
        assert leftover == 0
    finally:
        await owner.close()

    await asyncio.to_thread(upgrade, SCRATCH_DB)
    assert await _schema_diff(settings, SCRATCH_DB) == []


async def test_main_database_matches_models(settings: Settings, connect: Connect) -> None:
    assert await _schema_diff(settings, settings.pg_db) == []
