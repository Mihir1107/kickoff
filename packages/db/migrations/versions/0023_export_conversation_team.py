"""Per-conversation Slack team for export identities (ADR 0014 section 7, ADR 0004 amendment).

- ``export_conversations.team_id``: the team id the conversation's own metadata record carries
  (``context_team_id``, else ``team_id``/``team``; *confirm on real export*, Enterprise Grid). Items of
  the conversation are namespaced by it; NULL falls back to the export's workspace (users.json).

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0023"
down_revision: str | None = "0022"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE export_conversations ADD COLUMN team_id text;
"""

DOWNGRADE = """
ALTER TABLE export_conversations DROP COLUMN team_id;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
