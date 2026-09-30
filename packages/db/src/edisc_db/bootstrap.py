"""Cluster-level bootstrap, run as a superuser BEFORE migrations (idempotent).

Creates the two login roles and the schema. Everything inside the schema (tables, RLS, triggers,
grants) is created by Alembic running as the owner role. In production this step belongs to infra
provisioning; locally/CI ``make migrate`` runs it.

- ``edisc_owner``: owns the schema and all objects; used only by migrations.
- ``edisc_app``: API and workers. NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, not a member
  of the owner role, cannot create objects in the schema.
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from edisc_core.settings import Settings, get_settings


async def _ensure_role(conn: asyncpg.Connection, name: str, password: str) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    verb = "ALTER" if exists else "CREATE"
    sql = await conn.fetchval(
        "SELECT format('%s ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
        " NOREPLICATION NOINHERIT', $1::text, $2::text, $3::text)",
        verb,
        name,
        password,
    )
    await conn.execute(sql)


async def bootstrap(settings: Settings, *, db: str | None = None) -> None:
    database = db or settings.pg_db
    owner, app, schema = settings.pg_owner_user, settings.pg_app_user, settings.pg_schema
    conn = await asyncpg.connect(settings.pg_dsn("superuser", db="postgres"))
    try:
        await _ensure_role(conn, owner, settings.pg_owner_password.get_secret_value())
        await _ensure_role(conn, app, settings.pg_app_password.get_secret_value())
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database):
            await conn.execute(
                await conn.fetchval("SELECT format('CREATE DATABASE %I', $1::text)", database)
            )
    finally:
        await conn.close()

    conn = await asyncpg.connect(settings.pg_dsn("superuser", db=database))
    try:
        stmts = [
            "REVOKE ALL ON DATABASE %1$I FROM PUBLIC",
            "GRANT CONNECT, TEMPORARY ON DATABASE %1$I TO %2$I, %3$I",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            "CREATE SCHEMA IF NOT EXISTS %4$I AUTHORIZATION %2$I",
            "ALTER SCHEMA %4$I OWNER TO %2$I",
            "ALTER ROLE %2$I IN DATABASE %1$I SET search_path = %4$I, pg_temp",
            "ALTER ROLE %3$I IN DATABASE %1$I SET search_path = %4$I, pg_temp",
        ]
        for stmt in stmts:
            sql = await conn.fetchval(
                "SELECT format($1::text, $2::text, $3::text, $4::text, $5::text)",
                stmt,
                database,
                owner,
                app,
                schema,
            )
            await conn.execute(sql)
    finally:
        await conn.close()


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(bootstrap(get_settings(), db=db))


if __name__ == "__main__":
    main()
