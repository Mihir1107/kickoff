"""ADR 0007: tenant A cannot read or write tenant B rows through the app role (FORCE RLS)."""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import select

from edisc_core.settings import Settings
from edisc_db.models import Matter
from edisc_db.session import create_engine, session_factory, tenant_tx

from .conftest import ALL_TABLES, TENANT_TABLES, Connect, Seeded, tenant_ctx

# Per table: overrides that make a copy of tenant A's row distinct, so only RLS can reject it.
COPY_OVERRIDES = {
    "work_units": "'unit_key', 'X/' || gen_random_uuid()",
    "job_items": "'item_id', gen_random_uuid()",
    "custody_chain_heads": "'stream_id', gen_random_uuid()",
    "item_derivations": "'normalizer_version', gen_random_uuid()::text",
    "work_unit_scopes": "'scope_id', gen_random_uuid()",
    "group_members": "'id', gen_random_uuid()",
    "api_idempotency": "'key', gen_random_uuid()::text",
    "export_upload_parts": "'export_id', gen_random_uuid()",
    "export_entries": "'export_id', gen_random_uuid()",
    "export_conversations": "'export_id', gen_random_uuid()",
}
UPDATABLE = {
    "matters": "name",
    "connections": "status",
    "custodians": "display_name",
    "custodian_identities": "email",
    "collection_jobs": "status",
    "work_units": "cursor",
    "custody_chain_heads": "last_hash",
    "token_refresh_journal": "state",
    "slack_exports": "updated_at",
    "export_upload_parts": "updated_at",
}


def _tenant_col(table: str) -> str:
    return "id" if table == "tenants" else "tenant_id"


async def test_superuser_sees_both_tenants_so_zero_rows_below_is_not_vacuous(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, b = two_tenants
    conn = await connect("superuser")
    try:
        for table in ALL_TABLES:
            col = _tenant_col(table)
            n = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE {col} = ANY($1)", [a.tenant_id, b.tenant_id]
            )
            assert n >= 2, table
    finally:
        await conn.close()


@pytest.mark.parametrize("table", [*ALL_TABLES, "checkpoints", "reconciliation"])
async def test_reads_are_confined_to_the_current_tenant(
    connect: Connect, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    a, b = two_tenants
    conn = await connect("app")
    try:
        async with tenant_ctx(conn, a.tenant_id):
            col = _tenant_col(table)
            other = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE {col} = $1", b.tenant_id
            )
            foreign = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE {col} <> $1", a.tenant_id
            )
            mine = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE {col} = $1", a.tenant_id
            )
        assert other == 0
        assert foreign == 0
        assert mine >= 1
    finally:
        await conn.close()


@pytest.mark.parametrize("table", [*ALL_TABLES, "checkpoints", "reconciliation"])
async def test_no_tenant_context_sees_nothing(
    connect: Connect, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    conn = await connect("app")
    try:
        assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0
    finally:
        await conn.close()


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_cannot_insert_rows_for_another_tenant(
    connect: Connect, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    a, b = two_tenants
    extra = COPY_OVERRIDES.get(table, "'id', gen_random_uuid()")
    conn = await connect("app")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute(
                    f"""INSERT INTO {table}
                        SELECT (jsonb_populate_record(NULL::{table},
                                to_jsonb(r) || jsonb_build_object('tenant_id', $1::uuid, {extra}))).*
                        FROM {table} r LIMIT 1""",
                    b.tenant_id,
                )
    finally:
        await conn.close()


async def test_cannot_insert_without_tenant_context(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    conn = await connect("app")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
            await conn.execute(
                "INSERT INTO custodians (id, tenant_id, display_name) VALUES (gen_random_uuid(), $1, 'x')",
                a.tenant_id,
            )
    finally:
        await conn.close()


@pytest.mark.parametrize(("table", "column"), sorted(UPDATABLE.items()))
async def test_cannot_update_another_tenants_rows(
    connect: Connect, two_tenants: tuple[Seeded, Seeded], table: str, column: str
) -> None:
    a, b = two_tenants
    conn = await connect("app")
    try:
        async with tenant_ctx(conn, a.tenant_id):
            status = await conn.execute(
                f"UPDATE {table} SET {column} = {column} WHERE tenant_id = $1", b.tenant_id
            )
        assert status == "UPDATE 0"
    finally:
        await conn.close()


@pytest.mark.parametrize("table", ["collection_jobs", "work_units", "connections"])
async def test_cannot_move_own_rows_into_another_tenant(
    connect: Connect, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    a, b = two_tenants
    conn = await connect("app")
    try:
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute(
                    f"UPDATE {table} SET tenant_id = $1 WHERE tenant_id = $2",
                    b.tenant_id,
                    a.tenant_id,
                )
        # Either the RLS WITH CHECK or a tenant-composite FK stops it; both mean "cannot cross tenants".
        assert exc.value.sqlstate in {"42501", "23503"}
    finally:
        await conn.close()


async def test_create_tenant_restores_callers_context(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    conn = await connect("app")
    try:
        async with tenant_ctx(conn, a.tenant_id):
            await conn.fetchval(
                "SELECT create_tenant(gen_random_uuid(), 'x', 'ctx-' || substr(md5(random()::text), 1, 8), 'k')"
            )
            assert await conn.fetchval("SELECT current_tenant_id()") == a.tenant_id
    finally:
        await conn.close()


async def test_tenant_tx_helper_isolates(
    settings: Settings, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    engine = create_engine(settings, "app")
    sessions = session_factory(engine)
    try:
        async with tenant_tx(sessions, a.tenant_id) as s:
            tenants = set((await s.execute(select(Matter.tenant_id))).scalars())
        assert tenants == {a.tenant_id}
        # the setting is transaction-local: a later transaction on the same pool sees nothing
        async with sessions() as s:
            assert (await s.execute(select(Matter))).first() is None
    finally:
        await engine.dispose()


async def test_every_tenant_scoped_table_forces_rls_and_is_covered_here(connect: Connect) -> None:
    """Catalog check: a new table with a tenant_id must FORCE RLS with a tenant policy and be seeded
    by these tests (TENANT_TABLES), so no future table can silently skip isolation."""
    conn = await connect("superuser")
    try:
        rows = await conn.fetch(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
            " EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = 'tenant_isolation') AS policy"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped"
            " WHERE n.nspname = 'edisc' AND c.relkind = 'r'"
        )
    finally:
        await conn.close()
    tables = {r["relname"] for r in rows}
    assert tables == set(TENANT_TABLES), tables ^ set(TENANT_TABLES)
    for r in rows:
        assert r["relrowsecurity"] and r["relforcerowsecurity"] and r["policy"], r["relname"]
