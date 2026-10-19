"""Bulk-load reads stay bounded whatever the planner's statistics say (ADR 0015 §24.20).

Right after a bulk load, a table's statistics do not know the new rows: an export's freshly loaded
``export_entries`` are estimated at about one row, and the planner was seen to read ALL of an
export's remaining day entries and sort them for every page of the day-file pass (quadratic; 7.9 M
index tuples for a 66,000-entry export on CI). These tests put a database migrated from scratch
into each statistics state the product meets (never analyzed; analyzed before the export arrived;
analyzed after) and run the product's own page read as the app role, with custom and with generic
plans, counting the tuples the transaction read (``pg_stat_xact_user_tables``). Each stale state
first proves the trap is armed: the bare query, without the product's guard, reads the remainder.

The same holds for the foreign-key check of every day-file row (``export_id = $1 AND idx = $2`` on
``export_entries``, run by PostgreSQL per row): on stale statistics it was planned on
``(export_id, kind, folder)`` and read the export's entries up to the row (643 M index tuples for
66,000 entries on CI). The product's guard is ``analyze_export_index`` (migration 0044), called by
validation right after loading the entries.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_core.settings import Settings
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import upgrade
from edisc_db.session import create_engine, session_factory, tenant_tx, warm_pool
from edisc_normalizer.store import ITEMS_BY_KEY, NEXT_VERSIONS, PRIOR_STATE
from edisc_worker.exports import DAY_FILES_INSERT, DAY_FILES_PAGE, day_files_page
from edisc_worker.pipeline import _EARLIER_CLEAN_ITEMS

from .conftest import Connect

SCRATCH_DB = "edisc_stale_statistics"
TENANT = uuid.UUID("00000000-0000-7000-8000-000000000001")
ENTRIES = 40_000  # one in 50 is not a day file (metadata), the rest are day files
PAGE = 500
# a page reads its 500 day files plus the few non-day entries between them, never the remainder
BOUND = PAGE + PAGE // 25 + 10

LOAD = """
INSERT INTO export_entries (tenant_id, export_id, idx, name, folded_name, kind, folder, hint_day,
  method, crc32, compressed_size, uncompressed_size, local_header_offset, raw_name, name_encoding,
  flags)
SELECT $1, $2, i, 'c' || (i % 40) || '/' || i, 'c' || (i % 40) || '/' || i,
  CASE WHEN i % 50 = 0 THEN 'metadata' ELSE 'day' END,
  CASE WHEN i % 50 = 0 THEN NULL ELSE 'c' || (i % 40) END,
  CASE WHEN i % 50 = 0 THEN NULL ELSE date '2026-01-01' + (i % 300) END,
  8, 0, 10, 10, i * 100, ''::bytea, 'utf-8', 0
FROM generate_series(1, $3) i
"""
EXPORT_ROW = """
INSERT INTO slack_exports (id, tenant_id, client_id, declared_size, limits, staging_key, created_by)
VALUES ($2, $1, $1, 1, '{}', 'staging', 'test')
"""


@pytest.fixture
async def scratch(connect: Connect, settings: Settings) -> AsyncIterator[asyncpg.Connection]:
    """A database migrated from scratch here (so a migration's indexes count), its entries table
    never analyzed and kept that way (autovacuum off on it); a superuser connection that loads
    rows without their parents (``session_replication_role = replica``: the page read is all that
    is measured)."""
    su = await connect("superuser", db="postgres")
    try:
        await su.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    finally:
        await su.close()
    await bootstrap(settings, db=SCRATCH_DB)
    await asyncio.to_thread(upgrade, SCRATCH_DB)
    conn = await connect("superuser", db=SCRATCH_DB)
    await conn.execute("SET session_replication_role = replica")
    await conn.execute("ALTER TABLE export_entries SET (autovacuum_enabled = false)")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def app(
    settings: Settings, scratch: asyncpg.Connection
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(settings, "app", db=SCRATCH_DB, pool_size=1)
    await warm_pool(engine)
    try:
        yield session_factory(engine)
    finally:
        await engine.dispose()


async def _tuples_read(s: AsyncSession) -> int:
    row = (
        await s.execute(
            text(
                "SELECT coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) AS n"
                " FROM pg_stat_xact_user_tables WHERE relname = 'export_entries'"
            )
        )
    ).one()
    return int(row.n)


async def _page_work(
    sessions: async_sessionmaker[AsyncSession], export_id: uuid.UUID, after: int, *,
    generic: bool, guarded: bool = True,
) -> tuple[int, int]:  # fmt: skip
    """(rows returned, tuples the transaction read) for one page after ``after``."""
    async with tenant_tx(sessions, TENANT) as s:
        mode = "force_generic_plan" if generic else "force_custom_plan"
        await s.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
        before = await _tuples_read(s)
        if guarded:
            rows = await day_files_page(s, export_id, after)
        else:  # the bare query: what the planner does on its own. Its own TEXT: a prepared
            # statement's cached generic plan is not re-planned when a setting changes, so running
            # the product's text unguarded here would hand the guarded call a plan made with sorts
            bare = DAY_FILES_PAGE + " -- unguarded"
            rows = (await s.execute(text(bare), {"e": export_id, "after": after})).all()
        return len(rows), await _tuples_read(s) - before


async def _assert_bounded(
    sessions: async_sessionmaker[AsyncSession], export_id: uuid.UUID, state: str
) -> None:
    # pages at the start, the middle and near the end of the export, in both plan modes
    for after in (0, ENTRIES * 100 // 2, ENTRIES * 100 - 2 * PAGE * 100):
        for generic in (False, True):
            n, work = await _page_work(sessions, export_id, after, generic=generic)
            assert n == PAGE, (state, after, generic, n)
            assert work <= BOUND, f"{state}: page after {after} (generic={generic}) read {work}"


async def _assert_trap_armed(
    sessions: async_sessionmaker[AsyncSession], export_id: uuid.UUID, state: str
) -> None:
    """The precondition: in this state the planner, left alone, reads far more than a page (so
    the bounded result above is the guard's doing, not a lucky plan)."""
    worst = 0
    for generic in (False, True):
        _, work = await _page_work(sessions, export_id, 0, generic=generic, guarded=False)
        worst = max(worst, work)
    assert worst > 10 * BOUND, f"{state}: the bare query read only {worst}: state not reproduced"


def test_the_page_query_runs_only_through_its_guard() -> None:
    """``DAY_FILES_PAGE`` is executed by ``day_files_page`` alone: the same text run elsewhere
    without ``enable_sort = off`` on a pooled connection could leave a cached generic plan with a
    sort that the guarded call would then reuse."""
    root = Path(__file__).resolve().parents[3]
    users = [
        str(p.relative_to(root))
        for d in ("packages", "apps", "workers")
        for p in (root / d).rglob("*.py")
        if "DAY_FILES_PAGE" in p.read_text()
    ]
    assert users == ["workers/collection/src/edisc_worker/exports.py"]
    source = (root / users[0]).read_text()
    assert source.count("text(DAY_FILES_PAGE)") == 1  # day_files_page's execute, nothing else


async def test_day_file_pages_read_one_page_whatever_the_statistics(
    scratch: asyncpg.Connection, app: async_sessionmaker[AsyncSession]
) -> None:
    earlier, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await scratch.execute(LOAD, TENANT, earlier, 200)

    # 1. never analyzed: an export loaded into a table the statistics have never seen
    await scratch.execute(LOAD, TENANT, first, ENTRIES)
    await _assert_trap_armed(app, first, "never analyzed")
    await _assert_bounded(app, first, "never analyzed")

    # 2. stale: analyzed before this export arrived (it is in no statistic)
    await scratch.execute("ANALYZE export_entries")
    await scratch.execute(LOAD, TENANT, second, ENTRIES)
    await _assert_trap_armed(app, second, "analyzed before the export")
    await _assert_bounded(app, second, "analyzed before the export")

    # 3. fresh: analyzed with the export in the sample
    await scratch.execute("ANALYZE export_entries")
    await _assert_bounded(app, second, "analyzed after the export")


async def _insert_work(
    sessions: async_sessionmaker[AsyncSession], export_id: uuid.UUID, near: int, *, generic: bool
) -> int:
    """Tuples of ``export_entries`` one 500-row day-file insert read (its foreign-key checks), for
    the day entries from ``near`` on; rolled back, so it can be measured again."""
    entries = [i for i in range(near, near + PAGE + PAGE // 49 + 2) if i % 50 != 0][:PAGE]
    s = sessions()
    try:
        await s.begin()
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(TENANT)})
        mode = "force_generic_plan" if generic else "force_custom_plan"
        await s.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
        before = await _tuples_read(s)
        await s.execute(
            text(DAY_FILES_INSERT),
            {"t": TENANT, "e": export_id, "entry": entries, "elements": [1] * PAGE,
             "anomalies": [0] * PAGE, "error": [None] * PAGE},
        )  # fmt: skip
        return await _tuples_read(s) - before
    finally:
        await s.rollback()
        await s.close()


async def _assert_fk_checks(
    sessions: async_sessionmaker[AsyncSession], export_id: uuid.UUID, state: str, *, armed: bool
) -> None:
    """``armed``: this state must first show the unguarded checks reading far more than a row each
    (the never-analyzed state happens to plan them on the primary key: it is checked bounded only)."""
    near_end = ENTRIES - 2 * PAGE
    if armed:
        worst = max(
            [await _insert_work(sessions, export_id, near_end, generic=g) for g in (False, True)]
        )
        assert worst > 10 * BOUND, (
            f"{state}: unguarded, the checks read only {worst}: not reproduced"
        )
    async with tenant_tx(sessions, TENANT) as s:  # the product's guard, as the app role
        await s.execute(text("SELECT analyze_export_entries()"))
    for near in (1, ENTRIES // 2, near_end):
        for generic in (False, True):
            work = await _insert_work(sessions, export_id, near, generic=generic)
            assert work <= BOUND, f"{state}: 500 checks near {near} (generic={generic}) read {work}"


async def test_day_file_foreign_key_checks_read_one_row_each_after_the_guard(
    scratch: asyncpg.Connection, app: async_sessionmaker[AsyncSession]
) -> None:
    earlier, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for export_id in (earlier, first, second):
        await scratch.execute(EXPORT_ROW, TENANT, export_id)
    await scratch.execute(LOAD, TENANT, earlier, 200)

    # 1. never analyzed, then the guard
    await scratch.execute(LOAD, TENANT, first, ENTRIES)
    await _assert_fk_checks(app, first, "never analyzed", armed=False)

    # 2. statistics from before this export (the guard's own ANALYZE above), then the guard
    await scratch.execute(LOAD, TENANT, second, ENTRIES)
    await _assert_fk_checks(app, second, "analyzed before the export", armed=True)


UNITS = """
WITH u AS (
  INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day, status, recon_status)
  SELECT $1, gen_random_uuid(), 'C' || (i % 3000) || '/2026-01-' || lpad((1 + i % 28)::text, 2, '0'),
    'C' || (i % 3000), date '2026-01-01' + (i % 28), 'done', 'matched'
  FROM generate_series(1, $2) i
  RETURNING job_id, unit_key
), i AS (
  INSERT INTO items (id, tenant_id, job_id, source, source_item_id, version, item_type, content_hash,
    raw_hash, evidence_object_id, storage_key, json_path, connector_version, normalizer_version,
    idempotency_key)
  SELECT gen_random_uuid(), $1, u.job_id, 'slack', u.unit_key || '/' || u.job_id, 1, 'message',
    md5(u.job_id::text) || md5(u.unit_key), md5(u.job_id::text) || md5(u.unit_key), gen_random_uuid(),
    'k', '$', '0', '0', md5(u.unit_key) || md5(u.job_id::text)
  FROM u RETURNING id, job_id, source_item_id
)
INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id)
SELECT $1, i.job_id, i.id, split_part(i.source_item_id, '/', 1) || '/' || split_part(i.source_item_id, '/', 2),
  gen_random_uuid() FROM i
"""


async def _units_read(sessions: async_sessionmaker[AsyncSession], unit_key: str) -> int:
    async with tenant_tx(sessions, TENANT) as s:
        before = (
            (
                await s.execute(
                    text(
                        "SELECT coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) AS n"
                        " FROM pg_stat_xact_user_tables WHERE relname = 'work_units'"
                    )
                )
            )
            .one()
            .n
        )
        await s.execute(text(_EARLIER_CLEAN_ITEMS), {"u": unit_key, "j": uuid.uuid4()})
        after = (
            (
                await s.execute(
                    text(
                        "SELECT coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) AS n"
                        " FROM pg_stat_xact_user_tables WHERE relname = 'work_units'"
                    )
                )
            )
            .one()
            .n
        )
    return int(after) - int(before)


async def test_a_units_earlier_clean_collections_are_found_without_scanning_every_unit(
    scratch: asyncpg.Connection, app: async_sessionmaker[AsyncSession]
) -> None:
    """Absence detection asks, per unit, for the same unit's earlier clean collections. With
    30,000 other units in the table (any tenant's), each with its links and items as a collection
    leaves them, that reads the unit's own clean rows only."""
    await scratch.execute(UNITS, TENANT, 30_000)
    unit = "C7/2026-01-08"  # i % 3000 == 7 and i % 28 == 7: 10 of the 30,000 rows
    assert await _units_read(app, unit) <= 20
    # the measurement sees a scan when there is one: without the index, the whole table
    await scratch.execute("DROP INDEX ix_work_units_tenant_id_unit_key")
    assert await _units_read(app, unit) >= 30_000


# ------------------------------------------------------------------ a tenant new to the statistics
ITEMS = """
INSERT INTO items (id, tenant_id, job_id, source, source_item_id, version, item_type, content_hash,
  raw_hash, evidence_object_id, storage_key, json_path, connector_version, normalizer_version,
  idempotency_key, sent_at)
SELECT gen_random_uuid(), $1::uuid, $2::uuid, 'slack', 'T1/C' || (i % 50) || '/' || i, 1, 'message',
  md5($1::text || i) || md5(i::text), md5(i::text) || md5($1::text || i), gen_random_uuid(), 'k', '$',
  '0', '0', md5('ik' || $1::text || i) || md5(i::text || 'ik'),
  timestamptz '2026-01-01' + i * interval '1 second'
FROM generate_series(1, $3) i
"""
BATCH = 500
# per statement of one batch: its own keys (and a version or a change stream each), plus slack
ITEMS_BOUND = 2 * BATCH + 50


async def _items_read(s: AsyncSession) -> int:
    row = (
        await s.execute(
            text(
                "SELECT coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) AS n"
                " FROM pg_stat_xact_user_tables WHERE relname = 'items'"
            )
        )
    ).one()
    return int(row.n)


async def _batch_work(
    sessions: async_sessionmaker[AsyncSession], tenant: uuid.UUID, n: int, *, generic: bool
) -> dict[str, int]:
    """Tuples of ``items`` each per-batch statement of the pipeline read, for the tenant's last
    500 items (the product's exact texts: the prior state, the existing keys, the next versions,
    and the foreign-key checks of 500 derivation rows); rolled back."""
    first = n - BATCH + 1
    sids = [f"T1/C{i % 50}/{i}" for i in range(first, n + 1)]
    s = sessions()
    out: dict[str, int] = {}
    try:
        await s.begin()
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        mode = "force_generic_plan" if generic else "force_custom_plan"
        await s.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
        keys = [
            r.idempotency_key
            for r in await s.execute(
                text("SELECT idempotency_key FROM items WHERE source_item_id = ANY(:i)"),
                {"i": sids},
            )
        ]  # (by the unique (tenant, source, sid, version) index: not measured)
        ids = [
            r.id
            for r in await s.execute(
                text("SELECT id FROM items WHERE source_item_id = ANY(:i)"), {"i": sids}
            )
        ]
        statements = {
            "prior_state": (PRIOR_STATE, {"t": tenant, "s": "slack", "ids": sorted({*sids, *(f"{x}#change" for x in sids)})}),
            "items_by_key": (ITEMS_BY_KEY, {"t": tenant, "k": keys}),
            "next_versions": (NEXT_VERSIONS, {"t": tenant, "s": "slack", "ids": sids}),
            "fk_checks": (
                "INSERT INTO item_derivations (tenant_id, item_id, normalizer_version, derived, derived_hash)"
                " SELECT :t, i, 'x', '{}', repeat('a', 64) FROM unnest(CAST(:i AS uuid[])) AS i",
                {"t": tenant, "i": ids},
            ),
        }  # fmt: skip
        for name, (sql, params) in statements.items():
            before = await _items_read(s)
            await s.execute(text(sql), params)
            out[name] = await _items_read(s) - before
        return out
    finally:
        await s.rollback()
        await s.close()


async def test_a_new_tenants_item_lookups_read_their_batch_after_the_refresh(
    scratch: asyncpg.Connection, app: async_sessionmaker[AsyncSession]
) -> None:
    """A tenant the statistics do not know yet (its first collection): another tenant's items are
    in the statistics, this tenant's 30,000 are not. Unguarded, the pipeline's per-batch statements
    read the tenant's items far beyond their batch (the trap, proven first); after the product's
    refresh (``analyze_collected_items``, called by the pipeline as a job's items double) each reads
    about its own batch, with custom and with generic plans."""
    known, fresh = uuid.uuid4(), uuid.uuid4()
    await scratch.execute(ITEMS, known, uuid.uuid4(), 30_000)
    async with tenant_tx(app, TENANT) as s:  # the state the refresh leaves: `known` analyzed
        await s.execute(text("SELECT analyze_collected_items()"))
    await scratch.execute(ITEMS, fresh, uuid.uuid4(), 30_000)

    unguarded = [await _batch_work(app, fresh, 30_000, generic=g) for g in (False, True)]
    worst = max(v for run in unguarded for v in run.values())
    assert worst > 10 * ITEMS_BOUND, f"the stale state is not reproduced: {unguarded}"

    # the collection goes on (its items double, the pipeline refreshes): at a size where reading
    # the tenant is never cheaper than its batch (at 60,000 rows a sequential scan of the table
    # costs about what 1,000 index probes do, and the planner fairly picks it)
    await scratch.execute(
        ITEMS.replace("FROM generate_series(1, $3) i", "FROM generate_series(30001, $3) i"),
        fresh, uuid.uuid4(), 300_000,
    )  # fmt: skip
    async with tenant_tx(app, TENANT) as s:
        await s.execute(text("SELECT analyze_collected_items()"))
    for generic in (False, True):
        work = await _batch_work(app, fresh, 300_000, generic=generic)
        assert all(v <= ITEMS_BOUND for v in work.values()), (generic, work, unguarded)
