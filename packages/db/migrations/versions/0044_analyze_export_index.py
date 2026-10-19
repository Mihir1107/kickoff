"""Bulk loads read bounded work whatever the statistics say (ADR 0015 §24.20).

- ``analyze_export_index(table)``: ANALYZE one of the export index tables (``export_entries``,
  ``export_conversations``, ``export_day_files``, ``export_threads``; anything else is refused).
  Validation calls it right after loading each, before anything reads them or references them.

Why: right after a bulk load the planner's statistics do not know the new export, every path is
estimated at about one row, and the foreign-key check of each ``export_day_files`` row
(``export_id = $1 AND idx = $2`` on ``export_entries``) was planned on ``(export_id, kind, folder)``
with ``idx`` as a filter: it read the export's entries up to that row, per row. On CI that was
643 M index tuples for a 66,000-entry export (quadratic; the zip64 test's timeouts). After an
ANALYZE the export's size is known and the primary key wins (1 row against thousands). The app
role may not ANALYZE (owner only, PostgreSQL 16): this definer function, owned by the tables'
owner, does that one thing. ANALYZE waits for a conflicting ANALYZE or VACUUM of the same table
(autovacuum is cancelled after ``deadlock_timeout``); ``lock_timeout`` 30 s bounds that wait, after
which the caller's activity is retried.

- ``ix_work_units_tenant_id_unit_key`` (partial: clean conversation-day units): absence detection
  asks, per unit, for the same unit's earlier CLEAN collections (``pipeline._EARLIER_CLEAN_ITEMS``:
  ``unit_key = :u AND job_id <> :j`` and the clean predicate). No index led with ``unit_key``, so
  every unit scanned the whole ``work_units`` table, every tenant's (found by the auto_explain
  sweep, 2026-10-19): quadratic in the units ever collected, whatever the statistics.

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0044"
down_revision: str | None = "0043"
branch_labels = None
depends_on = None


def _upgrade(app: str) -> str:
    return f"""
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
CREATE INDEX ix_work_units_tenant_id_unit_key ON work_units (tenant_id, unit_key)
    WHERE kind = 'conversation_day' AND status = 'done' AND recon_status = 'matched' AND file_gaps = 0;
"""


def _downgrade(app: str) -> str:
    return """
DROP INDEX IF EXISTS ix_work_units_tenant_id_unit_key;
DROP FUNCTION IF EXISTS analyze_export_index(text);
"""


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
