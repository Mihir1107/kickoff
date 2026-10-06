"""The connection's blind spots, recorded when it is created (ADR 0018 §7.2); the index the report's
`units.jsonl` order needs (§12).

`job_started` records a job's access facts (plan tier, granted scopes, blind spots, the unit day
zone) so the collection report can state them from the verified chain. Plan tier and granted scopes
already live on the connection; its blind spots were only in the `audit.connection_created` payload.
``connections.blind_spots`` is NULL for connections created before this revision: their jobs record
``blind_spots: null`` and the report prints UNKNOWN (no lookup in the audit stream, by decision D7).

``ix_work_units_job_id_conversation_id_day_unit_key``: `units.jsonl` is written in (conversation, day,
unit key) order in keyset pages; without it every page sorts all of the job's units (100k units took
103 s instead of about 10x the 10k time, docs/runs/2026-10-06-report-scale.md).

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0030"
down_revision: str | None = "0029"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE connections ADD COLUMN blind_spots text[];
CREATE INDEX ix_work_units_job_id_conversation_id_day_unit_key
    ON work_units (job_id, conversation_id, day, unit_key);
"""

DOWNGRADE = """
DROP INDEX IF EXISTS ix_work_units_job_id_conversation_id_day_unit_key;
ALTER TABLE connections DROP COLUMN IF EXISTS blind_spots;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
