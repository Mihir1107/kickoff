"""Visible stuck sealing of renders (ADR 0015 §15).

A render's seal is retried without limit (a render never ends without a sealed record). Each failed
attempt is counted (``seal_failures``, ``last_seal_error``); once the failures reach
``EDISC_RENDER_SEAL_STUCK_ATTEMPTS`` or the render has been final for longer than
``EDISC_RENDER_SEAL_STUCK_SECONDS`` without a seal, ``sealing_stuck_at`` is set once and an alert is
raised. The timestamp stays after a later seal succeeds (history); the API derives the state.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0027"
down_revision: str | None = "0026"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE renders ADD COLUMN seal_failures integer NOT NULL DEFAULT 0,
    ADD COLUMN last_seal_error text,
    ADD COLUMN sealing_stuck_at timestamptz,
    ADD CONSTRAINT ck_renders_sealing CHECK (
        seal_failures >= 0 AND (sealing_stuck_at IS NULL OR status IN ('completed', 'refused', 'failed')));
"""

DOWNGRADE = """
ALTER TABLE renders DROP CONSTRAINT IF EXISTS ck_renders_sealing, DROP COLUMN IF EXISTS sealing_stuck_at,
    DROP COLUMN IF EXISTS last_seal_error, DROP COLUMN IF EXISTS seal_failures;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
