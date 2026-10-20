"""Legal-hold rules that live in the database (migration 0047, ADR 0019 §4), checked on a database
migrated from scratch HERE, so a change to the migration counts: the one definition of "held"
(matter, client, direct client, an object through every owner), the close trigger on any path, the
two-person CHECK.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import upgrade

from .conftest import Connect

SCRATCH_DB = "edisc_hold_rules"


@pytest.fixture(scope="module")
async def db(connect: Connect, settings: Settings) -> AsyncIterator[asyncpg.Connection]:
    su = await connect("superuser", db="postgres")
    try:
        await su.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    finally:
        await su.close()
    await bootstrap(settings, db=SCRATCH_DB)
    await asyncio.to_thread(upgrade, SCRATCH_DB)
    conn = await connect("superuser", db=SCRATCH_DB)
    await conn.execute("SET search_path = edisc, pg_temp")
    try:
        yield conn
    finally:
        await conn.close()


async def _world(c: asyncpg.Connection) -> dict[str, uuid.UUID]:
    """Two matters (A, B) of one client, a job each; B's job links an item behind an object of
    A's job (dedup across matters); an export of the client. Parents skipped (replica mode)."""
    ids = {k: new_id() for k in ("t", "client", "a", "b", "ja", "jb", "obj", "own_b", "item",
                                  "export_obj", "export")}  # fmt: skip
    await c.fetchval(
        "SELECT create_tenant($1, 'holds', 'holds-' || substr(md5(random()::text), 1, 8), 'k')",
        ids["t"],
    )
    await c.execute("SET session_replication_role = replica")
    try:
        await c.execute(
            "INSERT INTO clients (id, tenant_id, name) VALUES ($1, $2, 'c')",
            ids["client"],
            ids["t"],
        )
        for m in ("a", "b"):
            await c.execute(
                "INSERT INTO matters (id, tenant_id, client_id, name, retention_until)"
                " VALUES ($1, $2, $3, 'm', now() + interval '1 year')",
                ids[m],
                ids["t"],
                ids["client"],
            )
        for j, m in (("ja", "a"), ("jb", "b")):
            await c.execute(
                "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status,"
                " connector_version, requested_by) VALUES ($1, $2, $3, $4, 'running', '1', 't')",
                ids[j],
                ids["t"],
                ids[m],
                new_id(),
            )
        for o, j in (("obj", "ja"), ("own_b", "jb")):
            await c.execute(
                "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until,"
                " source_sha256, source_hash_origin) VALUES ($1, $2, $3, $4, 'page',"
                " now() + interval '1 day', $5, 'collection')",
                ids[o],
                ids["t"],
                ids[j],
                f"k/{ids[o]}",
                "0" * 64,
            )
        await c.execute(
            "INSERT INTO evidence_objects (id, tenant_id, storage_key, kind, retain_until,"
            " source_sha256, source_hash_origin) VALUES ($1, $2, $4, 'page',"
            " now() + interval '1 day', $3, 'collection')",
            ids["export_obj"],
            ids["t"],
            "0" * 64,
            f"k/{ids['export_obj']}",
        )
        await c.execute(
            "INSERT INTO items (id, tenant_id, job_id, source, source_item_id, version, item_type,"
            " content_hash, raw_hash, evidence_object_id, storage_key, json_path, connector_version,"
            " normalizer_version, idempotency_key) VALUES ($1, $2, $3, 'dummy', 'C1/1', 1, 'message',"
            " $4, $4, $5, 'k', '$.m', '1', '1', $6)",
            ids["item"],
            ids["t"],
            ids["ja"],
            "0" * 64,
            ids["obj"],
            new_id().hex * 2,  # 64 hex characters
        )
        await c.execute(
            "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id)"
            " VALUES ($1, $2, $3, 'u', $4)",
            ids["t"],
            ids["jb"],
            ids["item"],
            new_id(),
        )
        await c.execute(
            "INSERT INTO slack_exports (id, tenant_id, client_id, declared_size, limits, staging_key,"
            " created_by, evidence_object_id) VALUES ($1, $2, $3, 1, '{}', 's', 't', $4)",
            ids["export"],
            ids["t"],
            ids["client"],
            ids["export_obj"],
        )
    finally:
        await c.execute("SET session_replication_role = origin")
    return ids


async def _hold(c: asyncpg.Connection, t: uuid.UUID, *, matter: uuid.UUID | None = None,
                client: uuid.UUID | None = None) -> uuid.UUID:  # fmt: skip
    h = new_id()
    await c.execute(
        "INSERT INTO legal_holds (id, tenant_id, matter_id, client_id, reason, placed_by)"
        " VALUES ($1, $2, $3, $4, 'r', 'p')", h, t, matter, client)  # fmt: skip
    return h


async def _release(c: asyncpg.Connection, h: uuid.UUID) -> None:
    await c.execute(
        "UPDATE legal_holds SET released_at = now(), released_by = 'x', release_reason = 'x',"
        " cooling_off_until = now() + interval '7 days' WHERE id = $1", h)  # fmt: skip


async def _held(c: asyncpg.Connection, w: dict[str, uuid.UUID]) -> dict[str, bool]:
    row = await c.fetchrow(
        "SELECT matter_held($1) AS a, matter_held($2) AS b, client_held($3) AS client,"
        " client_directly_held($3) AS direct, evidence_held($4) AS obj, evidence_held($5) AS own_b,"
        " evidence_held($6) AS export",
        w["a"], w["b"], w["client"], w["obj"], w["own_b"], w["export_obj"])  # fmt: skip
    return dict(row)


async def test_held_through_every_owner(db: asyncpg.Connection) -> None:
    w = await _world(db)
    assert set((await _held(db, w)).values()) == {False}
    hb = await _hold(db, w["t"], matter=w["b"])
    assert await _held(db, w) == {"a": False, "b": True, "client": True, "direct": False,
                                  "obj": True,  # A's object, linked by B's job (dedup)
                                  "own_b": True, "export": False}  # fmt: skip
    await _release(db, hb)
    hc = await _hold(db, w["t"], client=w["client"])
    assert set((await _held(db, w)).values()) == {True}  # a client hold: both matters, the export
    await _release(db, hc)
    assert set((await _held(db, w)).values()) == {False}


async def test_closing_under_a_hold_is_refused_on_any_path(db: asyncpg.Connection) -> None:
    w = await _world(db)
    h = await _hold(db, w["t"], matter=w["a"])
    with pytest.raises(asyncpg.PostgresError) as exc:  # the superuser too: the trigger, not RLS
        await db.execute(
            "UPDATE matters SET closed_at = now(), closed_by = 'x' WHERE id = $1", w["a"]
        )
    assert exc.value.sqlstate == "EA005"
    await db.execute("UPDATE matters SET closed_at = now(), closed_by = 'x' WHERE id = $1", w["b"])
    with pytest.raises(asyncpg.PostgresError) as exc:  # a held matter keeps its client open
        await db.execute(
            "UPDATE clients SET closed_at = now(), closed_by = 'x' WHERE id = $1", w["client"]
        )
    assert exc.value.sqlstate == "EA005"
    await _release(db, h)
    await db.execute("UPDATE matters SET closed_at = now(), closed_by = 'x' WHERE id = $1", w["a"])


async def test_the_requester_never_approves_and_records_are_final(db: asyncpg.Connection) -> None:
    w = await _world(db)
    h = await _hold(db, w["t"], matter=w["a"])
    requester = new_id()
    await db.execute(
        "INSERT INTO legal_hold_release_requests (id, tenant_id, hold_id, requested_by,"
        " requested_by_principal, reason, expires_at) VALUES ($1, $2, $3, 'r', $4, 'x',"
        " now() + interval '72 hours')", new_id(), w["t"], h, requester)  # fmt: skip
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "UPDATE legal_hold_release_requests SET decision = 'approved', decided_at = now(),"
            " decided_by = 'r', decided_by_principal = $2 WHERE hold_id = $1", h, requester)  # fmt: skip
    with pytest.raises(asyncpg.PostgresError):
        await db.execute("UPDATE legal_holds SET reason = 'changed' WHERE id = $1", h)
    await _release(db, h)
    with pytest.raises(asyncpg.PostgresError):  # a release is final
        await db.execute("UPDATE legal_holds SET released_at = NULL, released_by = NULL,"
                         " release_reason = NULL, cooling_off_until = NULL WHERE id = $1", h)  # fmt: skip
