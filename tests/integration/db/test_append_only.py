"""Triggers enforce append-only / guarded mutation even for the OWNER role (independent of grants)."""

from __future__ import annotations

import asyncpg
import pytest

from edisc_core.ids import new_id

from .conftest import HEX, Connect, Seeded, seed_tenant, tenant_ctx

APPEND_ONLY = ["custody_events", "items", "job_items", "item_derivations"]
NO_TRUNCATE = [*APPEND_ONLY, "evidence_objects", "custody_chain_heads", "matters", "tenants"]


async def _expect_sqlstate(
    conn: asyncpg.Connection, tenant: Seeded, sql: str, sqlstate: str
) -> None:
    with pytest.raises(asyncpg.PostgresError) as exc:
        async with tenant_ctx(conn, tenant.tenant_id):
            await conn.execute(sql)
    assert exc.value.sqlstate == sqlstate, exc.value


@pytest.fixture
async def owner(connect: Connect) -> asyncpg.Connection:
    conn = await connect("owner")
    yield conn
    await conn.close()


@pytest.mark.parametrize("table", APPEND_ONLY)
async def test_update_rejected_even_for_owner(
    owner: asyncpg.Connection, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    await _expect_sqlstate(
        owner, two_tenants[0], f"UPDATE {table} SET tenant_id = tenant_id", "EA001"
    )


@pytest.mark.parametrize(
    "table", [*APPEND_ONLY, "evidence_objects", "custody_chain_heads", "matters", "tenants"]
)
async def test_delete_rejected_even_for_owner(
    owner: asyncpg.Connection, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    await _expect_sqlstate(owner, two_tenants[0], f"DELETE FROM {table}", "EA001")


@pytest.mark.parametrize("table", NO_TRUNCATE)
async def test_truncate_rejected_even_for_owner(
    owner: asyncpg.Connection, two_tenants: tuple[Seeded, Seeded], table: str
) -> None:
    before = await _count_all(owner, table, two_tenants[0])
    # CASCADE so FK checks cannot pre-empt the trigger: every cascaded table has a TRUNCATE trigger too.
    await _expect_sqlstate(owner, two_tenants[0], f"TRUNCATE {table} CASCADE", "EA001")
    assert await _count_all(owner, table, two_tenants[0]) == before


async def _count_all(conn: asyncpg.Connection, table: str, tenant: Seeded) -> int:
    async with tenant_ctx(conn, tenant.tenant_id):
        return int(await conn.fetchval(f"SELECT count(*) FROM {table}"))


async def test_evidence_objects_completion_requires_provenance_and_pinned_version(
    connect: Connect,
) -> None:
    app = await connect("app")
    try:
        s = await seed_tenant(app)
        ev = f"'{s.evidence_id}'"
        # the seeded object is complete: final (only retain_until may be extended)
        await _expect_sqlstate(
            app, s, f"UPDATE evidence_objects SET sha256 = '{'cd' * 32}' WHERE id = {ev}", "EA002"
        )
        await _expect_sqlstate(
            app, s, f"UPDATE evidence_objects SET version_id = 'other' WHERE id = {ev}", "EA002"
        )
        await _expect_sqlstate(
            app, s, f"UPDATE evidence_objects SET state = 'missing' WHERE id = {ev}", "EA002"
        )
        pending = new_id()
        pid = f"'{pending}'"
        async with tenant_ctx(app, s.tenant_id):
            await app.execute(
                "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until)"
                " VALUES ($1, $2, $3, $4, 'page', now() + interval '1 day')",
                pending,
                s.tenant_id,
                s.job_id,
                f"test/{pending}",
            )
        done = f"state = 'complete', sha256 = '{HEX}', size_bytes = 1, completed_at = now(), version_id = 'v1'"
        # no persisted source hash: completion refused (storage hashes never stand in for source hashes)
        await _expect_sqlstate(
            app, s, f"UPDATE evidence_objects SET {done} WHERE id = {pid}", "EA002"
        )
        async with tenant_ctx(app, s.tenant_id):
            await app.execute(
                "UPDATE evidence_objects SET source_sha256 = $2, source_hash_origin = 'collection' WHERE id = $1",
                pending,
                HEX,
            )
        # the source hash is write-once
        await _expect_sqlstate(
            app,
            s,
            f"UPDATE evidence_objects SET source_sha256 = '{'cd' * 32}' WHERE id = {pid}",
            "EA002",
        )
        # completing with a different hash, or without a pinned version, is refused
        wrong_hash = done.replace(HEX, "cd" * 32)
        unpinned = done.replace(", version_id = 'v1'", "")
        await _expect_sqlstate(
            app, s, f"UPDATE evidence_objects SET {wrong_hash} WHERE id = {pid}", "EA002"
        )
        await _expect_sqlstate(
            app, s, f"UPDATE evidence_objects SET {unpinned} WHERE id = {pid}", "EA002"
        )
        # immutable identity while pending
        await _expect_sqlstate(
            app, s, f"UPDATE evidence_objects SET storage_key = 'x' WHERE id = {pid}", "EA002"
        )
        async with tenant_ctx(app, s.tenant_id):
            assert (
                await app.execute(f"UPDATE evidence_objects SET {done} WHERE id = $1", pending)
                == "UPDATE 1"
            )
            # retention may be extended afterwards, never shortened
            await app.execute(
                "UPDATE evidence_objects SET retain_until = retain_until + interval '1 hour' WHERE id = $1",
                pending,
            )
        await _expect_sqlstate(
            app,
            s,
            f"UPDATE evidence_objects SET retain_until = retain_until - interval '2 hours' WHERE id = {pid}",
            "EA002",
        )
    finally:
        await app.close()


async def test_chain_head_guard(connect: Connect) -> None:
    app = await connect("app")
    try:
        s = await seed_tenant(app)  # head at seq 1, anchored 0
        sid = f"'{s.job_id}'"
        for bad in (
            "last_seq = last_seq + 2",
            "last_seq = last_seq - 1",
            f"last_hash = '{'cd' * 32}'",  # same seq, different hash: rewriting the head
            "last_seq = last_seq + 1, last_anchored_seq = 1",  # append may not touch anchor bookkeeping
            "last_anchored_seq = last_seq + 1",  # cannot anchor beyond the head (check constraint)
        ):
            with pytest.raises(asyncpg.PostgresError) as exc:
                async with tenant_ctx(app, s.tenant_id):
                    await app.execute(
                        f"UPDATE custody_chain_heads SET {bad} WHERE stream_id = {sid}"
                    )
            assert exc.value.sqlstate in {"EA003", "23514"}, bad
        async with tenant_ctx(app, s.tenant_id):
            await app.execute(
                "UPDATE custody_chain_heads SET last_anchored_seq = 1, anchor_due = false WHERE stream_id = $1",
                s.job_id,
            )
            await app.execute(
                "UPDATE custody_chain_heads SET last_seq = last_seq + 1 WHERE stream_id = $1",
                s.job_id,
            )
        await _expect_sqlstate(
            app,
            s,
            f"UPDATE custody_chain_heads SET last_anchored_seq = 0 WHERE stream_id = {sid}",
            "EA003",
        )
    finally:
        await app.close()


async def test_matter_retention_only_extends(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    app = await connect("app")
    try:
        await _expect_sqlstate(
            app,
            a,
            f"UPDATE matters SET retention_until = retention_until - interval '1 hour' WHERE id = '{a.matter_id}'",
            "EA004",
        )
        async with tenant_ctx(app, a.tenant_id):
            await app.execute(
                "UPDATE matters SET retention_until = retention_until + interval '1 hour' WHERE id = $1",
                a.matter_id,
            )
    finally:
        await app.close()


async def test_mutable_tables_stay_mutable(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    app = await connect("app")
    try:
        async with tenant_ctx(app, a.tenant_id):
            assert (
                await app.execute(
                    "UPDATE collection_jobs SET status = 'running' WHERE id = $1", a.job_id
                )
                == "UPDATE 1"
            )
            assert (
                await app.execute(
                    "UPDATE work_units SET cursor = 'p2', pages_done = pages_done + 1, updated_at = now() WHERE job_id = $1",
                    a.job_id,
                )
                == "UPDATE 1"
            )
            row = await app.fetchrow(
                "SELECT cursor, pages_done FROM checkpoints WHERE job_id = $1", a.job_id
            )
        assert row is not None
        assert row["cursor"] == "p2"
    finally:
        await app.close()


async def test_idempotency_key_unique_per_tenant(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    app = await connect("app")
    try:
        async with tenant_ctx(app, a.tenant_id):
            key = await app.fetchval("SELECT idempotency_key FROM items WHERE id = $1", a.item_id)
            inserted = await app.execute(
                "INSERT INTO items (id, tenant_id, job_id, source, source_item_id, version, item_type, content_hash, raw_hash,"
                " evidence_object_id, storage_key, json_path, connector_version, normalizer_version, idempotency_key)"
                " SELECT gen_random_uuid(), tenant_id, job_id, source, source_item_id, version + 1, item_type, content_hash,"
                " raw_hash, evidence_object_id, storage_key, json_path, connector_version, normalizer_version, idempotency_key"
                " FROM items WHERE id = $1 ON CONFLICT (tenant_id, idempotency_key) DO NOTHING",
                a.item_id,
            )
        assert key
        assert inserted == "INSERT 0 0"
    finally:
        await app.close()
