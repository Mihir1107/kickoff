"""Unit facts in the chain, a collation-independent report order, hostile export ids (ADR 0018 §28).

- ``work_units.gap_reasons``: why a unit is a gap although its counts may match (an export value that
  cannot be keyed: a conversation or team id, a ``thread_ts`` holding a lone surrogate). Recorded in
  the unit's custody event with its kind, conversation, scopes and access-lost reason.
- ``unit_key`` / ``conversation_id`` (``work_units``, ``work_unit_scopes``, ``job_items``) are COLLATE
  "C": `units.jsonl` and `observations.jsonl` are written in (conversation, day, unit key) and (unit
  key, item) order, which must not depend on the database host's locale (glibc ``en_US`` orders
  differently from code points); the offline verifier recomputes the files in code-point order. The
  indexes are rebuilt with the new collation; the two views over ``work_units`` are recreated.
- ``export_conversations.exact``: names and ids holding a lone surrogate, as JSON Pointer -> exact
  UTF-16 code units (the text columns hold the replacement text); ``refused``: why the conversation's
  messages cannot be collected as items (its id, its team, or the export's workspace holds a lone
  surrogate). Such a conversation is listed under a stand-in id and its units are reported as gaps.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0033"
down_revision: str | None = "0032"
branch_labels = None
depends_on = None

VIEWS = """
CREATE VIEW checkpoints WITH (security_invoker = true) AS
    SELECT tenant_id, job_id, unit_key, cursor, pages_done, updated_at FROM work_units;
CREATE VIEW reconciliation WITH (security_invoker = true) AS
    SELECT tenant_id, job_id, unit_key, expected_count, collected_count, recon_status AS status
    FROM work_units;
GRANT SELECT ON checkpoints, reconciliation TO edisc_app;
"""

UPGRADE = (
    """
DROP VIEW checkpoints;
DROP VIEW reconciliation;
ALTER TABLE work_units
    ALTER COLUMN unit_key TYPE text COLLATE "C",
    ALTER COLUMN conversation_id TYPE text COLLATE "C",
    ADD COLUMN gap_reasons text[] NOT NULL DEFAULT '{}';
ALTER TABLE work_unit_scopes ALTER COLUMN unit_key TYPE text COLLATE "C";
ALTER TABLE job_items ALTER COLUMN unit_key TYPE text COLLATE "C";
ALTER TABLE export_conversations
    ADD COLUMN exact jsonb,
    ADD COLUMN refused text,
    ADD CONSTRAINT ck_export_conversations_refused CHECK (refused IS NULL OR refused IN
        ('conversation_id_lone_surrogate', 'team_id_lone_surrogate', 'workspace_id_lone_surrogate'));
"""
    + VIEWS
)

DOWNGRADE = (
    """
DROP VIEW checkpoints;
DROP VIEW reconciliation;
ALTER TABLE export_conversations DROP CONSTRAINT ck_export_conversations_refused,
    DROP COLUMN refused, DROP COLUMN exact;
ALTER TABLE job_items ALTER COLUMN unit_key TYPE text COLLATE "default";
ALTER TABLE work_unit_scopes ALTER COLUMN unit_key TYPE text COLLATE "default";
ALTER TABLE work_units DROP COLUMN gap_reasons,
    ALTER COLUMN conversation_id TYPE text COLLATE "default",
    ALTER COLUMN unit_key TYPE text COLLATE "default";
"""
    + VIEWS
)


def upgrade() -> None:
    for statement in split_sql(UPGRADE):
        op.execute(statement)


def downgrade() -> None:
    for statement in split_sql(DOWNGRADE):
        op.execute(statement)
