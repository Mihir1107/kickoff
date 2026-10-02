"""Coalesced anchoring (anchor-storm fix, docs/runs/2026-10-01-audit-burst.md).

- ``anchoring_seq`` / ``anchoring_since``: an atomic claim. Exactly one writer anchors a stream at a
  time; others skip instead of each writing a WORM anchor. A claim older than
  ``EDISC_CUSTODY_ANCHOR_CLAIM_TIMEOUT_SECONDS`` is abandoned (killed claimer) and may be taken over,
  which the anchor sweeper does.
- ``pending_lifecycle_seq``: the latest lifecycle event, so finishing an anchor of an older seq keeps
  the stream due until the lifecycle event itself is covered.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE custody_chain_heads
    ADD COLUMN anchoring_seq bigint,
    ADD COLUMN anchoring_since timestamptz,
    ADD COLUMN pending_lifecycle_seq bigint NOT NULL DEFAULT 0,
    ADD CONSTRAINT ck_custody_chain_heads_claim CHECK ((anchoring_seq IS NULL) = (anchoring_since IS NULL));
"""

DOWNGRADE = """
ALTER TABLE custody_chain_heads DROP CONSTRAINT IF EXISTS ck_custody_chain_heads_claim,
    DROP COLUMN IF EXISTS pending_lifecycle_seq, DROP COLUMN IF EXISTS anchoring_since, DROP COLUMN IF EXISTS anchoring_seq;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
