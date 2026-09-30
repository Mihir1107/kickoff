"""The app role cannot change schema, disable RLS or triggers, bypass RLS, or destroy data."""

from __future__ import annotations

import asyncpg
import pytest

from edisc_core.settings import Settings

from .conftest import ALL_TABLES, Connect, Seeded, tenant_ctx

DDL_ATTEMPTS = [
    "ALTER TABLE {t} ADD COLUMN pwned int",
    "ALTER TABLE {t} DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE {t} DISABLE TRIGGER ALL",
    "ALTER TABLE {t} OWNER TO CURRENT_USER",
    "DROP POLICY tenant_isolation ON {t}",
    "CREATE POLICY pwned ON {t} USING (true)",
    "DROP TABLE {t}",
]


async def test_role_attributes(connect: Connect, settings: Settings) -> None:
    conn = await connect("superuser")
    try:
        row = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, rolinherit"
            " FROM pg_roles WHERE rolname = $1",
            settings.pg_app_user,
        )
        assert row is not None
        assert not any(row.values())
        assert not await conn.fetchval(
            "SELECT pg_has_role($1, $2, 'MEMBER')", settings.pg_app_user, settings.pg_owner_user
        )
        owned = await conn.fetchval(
            "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner WHERE r.rolname = $1",
            settings.pg_app_user,
        )
        assert owned == 0
        # the owner role is not a superuser either
        assert not await conn.fetchval(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = $1",
            settings.pg_owner_user,
        )
    finally:
        await conn.close()


@pytest.mark.parametrize("table", ALL_TABLES)
@pytest.mark.parametrize("template", DDL_ATTEMPTS)
async def test_app_cannot_alter_tables(connect: Connect, table: str, template: str) -> None:
    conn = await connect("app")
    try:
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError, match=r"must be owner|permission denied"
        ):
            await conn.execute(template.format(t=table))
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TRIGGER trg_items_no_update ON items",
        "DROP TRIGGER trg_custody_events_no_truncate ON custody_events",
        "ALTER TABLE custody_events DISABLE TRIGGER trg_custody_events_no_delete",
        "DROP FUNCTION reject_mutation() CASCADE",
        "CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
        "CREATE OR REPLACE FUNCTION current_tenant_id() RETURNS uuid LANGUAGE sql AS $$ SELECT NULL::uuid $$",
        "CREATE TABLE edisc.pwned (id int)",
        "CREATE TABLE public.pwned (id int)",
        "CREATE VIEW edisc.pwned AS SELECT 1",
        "SET session_replication_role = replica",
        "SET ROLE edisc_owner",
        "INSERT INTO tenants (id, name, subdomain, kms_key_ref) VALUES (gen_random_uuid(), 'x', 'pwned', 'k')",
        "SELECT * FROM alembic_version",
    ],
)
async def test_app_cannot_tamper_with_security_objects(connect: Connect, sql: str) -> None:
    conn = await connect("app")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(sql)
    finally:
        await conn.close()


async def test_app_cannot_turn_off_row_security(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    conn = await connect("app")
    try:
        await conn.execute("SET row_security = off")
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            await conn.fetch("SELECT * FROM items")
    finally:
        await conn.close()


@pytest.mark.parametrize("table", ALL_TABLES)
async def test_app_cannot_truncate_or_delete(
    connect: Connect, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    a, _ = two_tenants
    conn = await connect("app")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(f"TRUNCATE {table} CASCADE")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute(f"DELETE FROM {table}")
    finally:
        await conn.close()


@pytest.mark.parametrize("table", ["items", "custody_events", "job_items"])
async def test_app_has_no_update_on_append_only_tables(
    connect: Connect, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    a, _ = two_tenants
    conn = await connect("app")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute(f"UPDATE {table} SET tenant_id = tenant_id")
    finally:
        await conn.close()
