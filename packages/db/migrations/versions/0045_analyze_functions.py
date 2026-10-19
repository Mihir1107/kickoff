"""Statistics refreshes without a caller-supplied table, and a collection's items as they double
(ADR 0015 §24.20).

- Replaces ``analyze_export_index(text)`` (0044) with ZERO-ARGUMENT functions, each with a fixed
  list of schema-qualified tables in static SQL: the caller chooses WHEN, never WHAT.
  - ``analyze_export_entries()``: ``edisc.export_entries``.
  - ``analyze_export_tables()``: ``edisc.export_conversations``, ``edisc.export_day_files``,
    ``edisc.export_threads``.
  - ``analyze_collected_items()``: ``edisc.items``, ``edisc.job_items``, ``edisc.item_derivations``.

  Each one is SECURITY DEFINER (owned by the tables' owner: the app role may not ANALYZE),
  ``SET search_path = pg_catalog, edisc, pg_temp`` (and the names are qualified anyway), with
  ``lock_timeout`` 30 s and EXECUTE for the app role only.
- ``collection_jobs.items_inserted``: the items a job inserted, counted in each batch's
  transaction. When the count crosses a power of two (from 1,024 on), the batch that crossed it
  calls ``analyze_collected_items()`` after its commit. For a tenant the statistics do not know yet
  (its first collection), the planner otherwise estimated the tenant at about one row and read
  all the tenant's items for each foreign-key check and each per-batch lookup (quadratic; found by
  the 2026-10-19 sweep).

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0045"
down_revision: str | None = "0044"
branch_labels = None
depends_on = None

FUNCTIONS = {
    "analyze_export_entries": ("edisc.export_entries",),
    "analyze_export_tables": (
        "edisc.export_conversations",
        "edisc.export_day_files",
        "edisc.export_threads",
    ),
    "analyze_collected_items": ("edisc.items", "edisc.job_items", "edisc.item_derivations"),
}


def _function(name: str, tables: tuple[str, ...], app: str) -> str:
    body = "\n".join(f"    ANALYZE {t};" for t in tables)
    return f"""
CREATE FUNCTION {name}() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp
SET lock_timeout = '30s' AS $$
BEGIN
{body}
END
$$;
REVOKE ALL ON FUNCTION {name}() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {name}() TO "{app}";
"""


def _upgrade(app: str) -> str:
    return (
        "DROP FUNCTION analyze_export_index(text);\n"
        + "".join(_function(n, t, app) for n, t in FUNCTIONS.items())
        + "ALTER TABLE collection_jobs ADD COLUMN items_inserted bigint NOT NULL DEFAULT 0,"
        " ADD CONSTRAINT ck_collection_jobs_items_inserted CHECK (items_inserted >= 0);\n"
    )


def _downgrade(app: str) -> str:
    return (
        "ALTER TABLE collection_jobs DROP COLUMN items_inserted;\n"
        + "".join(f"DROP FUNCTION IF EXISTS {n}();\n" for n in FUNCTIONS)
        + f"""
CREATE FUNCTION analyze_export_index(p_table text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp
SET lock_timeout = '30s' AS $$
BEGIN
    IF p_table NOT IN ('export_entries', 'export_conversations', 'export_day_files', 'export_threads') THEN
        RAISE EXCEPTION 'analyze_export_index: not an export index table' USING ERRCODE = '22023';
    END IF;
    EXECUTE format('ANALYZE edisc.%I', p_table);
END
$$;
REVOKE ALL ON FUNCTION analyze_export_index(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analyze_export_index(text) TO "{app}";
"""
    )


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def _app() -> str:
    return str(op.get_context().config.attributes.get("app_role", "edisc_app"))  # type: ignore[union-attr]


def upgrade() -> None:
    _run(_upgrade(_app()))


def downgrade() -> None:
    _run(_downgrade(_app()))
