"""Migration 0015: clients own matters and connections (default client), subdomain lookup for the API."""

from __future__ import annotations

import asyncio
import secrets

import asyncpg
import pytest

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import upgrade

from .conftest import Connect, tenant_ctx

SCRATCH_DB = "edisc_hiertest"


async def _new_tenant(conn: asyncpg.Connection) -> tuple[object, str]:
    t, sub = new_id(), f"h-{secrets.token_hex(6)}"
    await conn.fetchval("SELECT create_tenant($1, 'H', $2, 'local:k')", t, sub)
    return t, sub


async def test_existing_rows_are_backfilled_into_a_default_client(
    connect: Connect, settings: Settings
) -> None:
    su = await connect("superuser", db="postgres")
    try:
        await su.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    finally:
        await su.close()
    await bootstrap(settings, db=SCRATCH_DB)
    await asyncio.to_thread(upgrade, SCRATCH_DB, "0014")
    su = await connect("superuser", db=SCRATCH_DB)
    tenants = [new_id(), new_id()]
    try:
        for t in tenants:
            await su.execute(
                "INSERT INTO tenants (id, name, subdomain, kms_key_ref) VALUES ($1, 'B', $2, 'k')",
                t,
                f"b-{secrets.token_hex(6)}",
            )
            await su.execute(
                "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES ($1, $2, 'M', now() + interval '1 day')",
                new_id(),
                t,
            )
            await su.execute(
                "INSERT INTO connections (id, tenant_id, source, external_org_id, status) VALUES ($1, $2, 'dummy', 'o', 'active')",
                new_id(),
                t,
            )
        await asyncio.to_thread(upgrade, SCRATCH_DB)
        rows = await su.fetch(
            "SELECT t.id, (SELECT count(*) FROM clients c WHERE c.tenant_id = t.id AND c.is_default) AS defaults,"
            " (SELECT count(*) FROM matters m JOIN clients c ON c.id = m.client_id AND c.is_default"
            "  WHERE m.tenant_id = t.id) AS matters,"
            " (SELECT count(*) FROM connections x JOIN clients c ON c.id = x.client_id AND c.is_default"
            "  WHERE x.tenant_id = t.id) AS connections FROM tenants t WHERE t.id = ANY($1)",
            tenants,
        )
        assert (
            sorted((r["defaults"], r["matters"], r["connections"]) for r in rows) == [(1, 1, 1)] * 2
        )
        forced = await su.fetch(
            "SELECT relname FROM pg_class WHERE relname IN ('tenants', 'matters', 'connections')"
            " AND relforcerowsecurity"
        )
        assert len(forced) == 3  # the backfill's temporary NO FORCE was undone
    finally:
        await su.close()


async def test_inserts_without_a_client_use_one_default_client_even_concurrently(
    connect: Connect,
) -> None:
    app = await connect("app")
    try:
        t, _ = await _new_tenant(app)
    finally:
        await app.close()

    async def insert_matter() -> None:
        conn = await connect("app")
        try:
            async with tenant_ctx(conn, t):  # type: ignore[arg-type]
                await conn.execute(
                    "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES ($1, $2, 'M', now() + interval '1 day')",
                    new_id(),
                    t,
                )
        finally:
            await conn.close()

    await asyncio.gather(*(insert_matter() for _ in range(5)))
    app = await connect("app")
    try:
        async with tenant_ctx(app, t):  # type: ignore[arg-type]
            clients = await app.fetch("SELECT id, is_default FROM clients")
            matters = await app.fetch("SELECT DISTINCT client_id FROM matters")
    finally:
        await app.close()
    assert len(clients) == 1 and clients[0]["is_default"]
    assert [m["client_id"] for m in matters] == [clients[0]["id"]]


async def test_subdomain_lookup_needs_no_tenant_context_and_reveals_only_an_id(
    connect: Connect,
) -> None:
    app = await connect("app")
    try:
        t, sub = await _new_tenant(app)
        assert await app.fetchval("SELECT tenant_id_for_subdomain($1)", sub) == t
        assert await app.fetchval("SELECT tenant_id_for_subdomain('no-such-tenant')") is None
        assert await app.fetchval("SELECT count(*) FROM tenants") == 0  # still no direct visibility
    finally:
        await app.close()


async def test_role_assignments_and_memberships_cannot_be_deleted(connect: Connect) -> None:
    app = await connect("app")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("DELETE FROM role_assignments")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("DELETE FROM group_members")
    finally:
        await app.close()
