"""Event kinds for unavailable files and lost conversation access (M10.1 / ADR 0004).

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None

_OLD = [
    "reaction_snapshot",
    "identity_snapshot",
    "change_observation",
    "no_longer_observed",
    "observed_again",
]
_NEW = [*_OLD, "file_unavailable", "file_became_available", "access_lost", "access_restored"]
KINDS_0009 = "(" + ", ".join(f"'{k}'" for k in _OLD) + ")"
KINDS_0010 = "(" + ", ".join(f"'{k}'" for k in _NEW) + ")"


def _check(kinds: str) -> str:
    return f"""
ALTER TABLE items DROP CONSTRAINT ck_items_event_kind;
ALTER TABLE items ADD CONSTRAINT ck_items_event_kind CHECK (
    (item_type = 'event') = (event_kind IS NOT NULL) AND (event_kind IS NULL OR event_kind IN {kinds}));
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_check(KINDS_0010))


def downgrade() -> None:
    _run(_check(KINDS_0009))
