"""Storage/throughput trims (docs/runs/2026-10-01-storage-throughput-breakdown.md, option 1).

- Drop ``items (tenant_id, source, source_item_id)``: a prefix of the unique
  ``(tenant_id, source, source_item_id, version)`` index, which serves the same lookups (~7% of the
  Postgres bytes per message and one less index to maintain on every insert).
- ``work_units.last_page_fragment_hash``: canonical hash of the last history page's message list, stored
  with the batch, so unit finalize no longer downloads that page again. NULL for units written before
  this revision; finalize then falls back to reading the page.
- Re-analyze the bulk-loaded tables after 2% change instead of 10%: with stale statistics during a load,
  the planner flipped the unit finalize queries to scans of a whole day's items (300 ms instead of 3 ms).

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0014"
down_revision: str | None = "0013"
branch_labels = None
depends_on = None

UPGRADE = """
DROP INDEX IF EXISTS ix_items_tenant_id_source_source_item_id;
ALTER TABLE work_units ADD COLUMN last_page_fragment_hash text;
ALTER TABLE items SET (autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE job_items SET (autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE item_derivations SET (autovacuum_analyze_scale_factor = 0.02);
"""

DOWNGRADE = """
ALTER TABLE items RESET (autovacuum_analyze_scale_factor);
ALTER TABLE job_items RESET (autovacuum_analyze_scale_factor);
ALTER TABLE item_derivations RESET (autovacuum_analyze_scale_factor);
ALTER TABLE work_units DROP COLUMN IF EXISTS last_page_fragment_hash;
CREATE INDEX IF NOT EXISTS ix_items_tenant_id_source_source_item_id ON items (tenant_id, source, source_item_id);
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
