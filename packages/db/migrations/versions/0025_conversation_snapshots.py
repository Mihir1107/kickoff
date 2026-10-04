"""Event kind ``conversation_snapshot`` (ADR 0004 amendment, 2026-10-04): versioned conversation
metadata (name, type, topic, purpose, members, archived, shared) from the directory unit, so renders
carry the conversation type and every name a channel had.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0025"
down_revision: str | None = "0024"
branch_labels = None
depends_on = None

_OLD = [
    "reaction_snapshot",
    "identity_snapshot",
    "change_observation",
    "no_longer_observed",
    "observed_again",
    "file_unavailable",
    "file_became_available",
    "access_lost",
    "access_restored",
]
_NEW = [*_OLD, "conversation_snapshot"]


def _check(kinds: list[str]) -> str:
    listed = "(" + ", ".join(f"'{k}'" for k in kinds) + ")"
    return f"""
ALTER TABLE items DROP CONSTRAINT ck_items_event_kind;
ALTER TABLE items ADD CONSTRAINT ck_items_event_kind CHECK (
    (item_type = 'event') = (event_kind IS NOT NULL) AND (event_kind IS NULL OR event_kind IN {listed}));
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_check(_NEW))


def downgrade() -> None:
    _run(_check(_OLD))
