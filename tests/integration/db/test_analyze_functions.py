"""The statistics refreshes are safe as SECURITY DEFINER (migration 0045, ADR 0015 §24.20).

They run as the tables' owner (the app role may not ANALYZE), so each must do exactly one fixed
thing: no argument, no dynamic SQL, a pinned ``search_path``, schema-qualified tables, EXECUTE for
the app role only. Checked on a database migrated from scratch here (so a change to the migration
counts), statically (the catalog) and by behaviour (which tables' ANALYZE counters move, a temp
table shadowing a name, a direct ANALYZE, another role, an argument).
"""

from __future__ import annotations

import re

import asyncpg
import pytest

from .conftest import Connect
from .test_stale_statistics import SCRATCH_DB, scratch  # noqa: F401 (the fixture)

EXPECTED = {
    "analyze_export_entries": {"export_entries"},
    "analyze_export_tables": {"export_conversations", "export_day_files", "export_threads"},
    "analyze_collected_items": {"items", "job_items", "item_derivations"},
}
APP = "edisc_app"


async def _analyze_counts(su: asyncpg.Connection) -> dict[str, int]:
    rows = await su.fetch(
        "SELECT relname, analyze_count FROM pg_stat_user_tables WHERE schemaname = 'edisc'"
    )
    return {r["relname"]: int(r["analyze_count"]) for r in rows}


async def test_each_function_is_fixed_and_pinned(scratch: asyncpg.Connection) -> None:  # noqa: F811
    rows = await scratch.fetch(
        "SELECT p.proname, p.pronargs, p.prosecdef, p.proconfig, p.prosrc, p.proacl::text[] AS acl,"
        " pg_get_userbyid(p.proowner) AS owner FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'edisc' AND p.proname LIKE 'analyze%'"
    )
    table_owner = await scratch.fetchval(
        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'edisc.items'::regclass"
    )
    assert {r["proname"] for r in rows} == set(EXPECTED)  # the old (text) one is gone
    for r in rows:
        name = r["proname"]
        assert r["pronargs"] == 0, f"{name} takes an argument"
        assert r["prosecdef"], name
        assert "search_path=pg_catalog, edisc, pg_temp" in (r["proconfig"] or []), name
        assert r["owner"] == table_owner, name
        source = r["prosrc"]
        assert not re.search(r"\b(EXECUTE|format)\b", source, re.I), f"{name}: dynamic SQL"
        targets = re.findall(r"\bANALYZE\s+([\w.]+)", source, re.I)
        assert set(targets) == {f"edisc.{t}" for t in EXPECTED[name]}, (name, targets)
        grantees = {entry.split("=", 1)[0] for entry in r["acl"] or []}
        assert grantees == {APP, r["owner"]}, (name, r["acl"])  # nobody else, never PUBLIC ("")


async def test_each_function_analyzes_exactly_its_tables(
    connect: Connect,
    scratch: asyncpg.Connection,  # noqa: F811
) -> None:
    app = await connect("app", db=SCRATCH_DB)
    try:
        for name, tables in EXPECTED.items():
            before = await _analyze_counts(scratch)
            await app.execute(f"SELECT {name}()")
            after = await _analyze_counts(scratch)
            moved = {t for t in after if after[t] != before.get(t, 0)}
            assert moved == tables, (name, moved)
    finally:
        await app.close()


async def test_the_app_role_cannot_point_it_elsewhere(
    connect: Connect,
    scratch: asyncpg.Connection,  # noqa: F811
) -> None:
    app = await connect("app", db=SCRATCH_DB)
    sweeper = await connect("sweeper", db=SCRATCH_DB)
    try:
        # a temp table shadowing a name changes nothing: the path is pinned, the name qualified
        await app.execute("CREATE TEMP TABLE items (x int)")
        await app.execute("SET search_path = pg_temp, edisc")
        before = await _analyze_counts(scratch)
        await app.execute("SELECT edisc.analyze_collected_items()")
        after = await _analyze_counts(scratch)
        assert after["items"] == before["items"] + 1
        temp_analyzed = await app.fetchval(
            "SELECT coalesce(analyze_count, 0) FROM pg_stat_all_tables"
            " WHERE relid = 'pg_temp.items'::regclass"
        )
        assert temp_analyzed == 0
        # no argument can name a table
        with pytest.raises(asyncpg.UndefinedFunctionError):
            await app.execute("SELECT edisc.analyze_collected_items('custody_events')")
        # the app role's own ANALYZE of another table does nothing (owner only)
        before = await _analyze_counts(scratch)
        await app.execute("ANALYZE edisc.custody_events")
        assert (await _analyze_counts(scratch))["custody_events"] == before["custody_events"]
        # no other role may call it
        for name in EXPECTED:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await sweeper.execute(f"SELECT edisc.{name}()")
    finally:
        await app.close()
        await sweeper.close()
