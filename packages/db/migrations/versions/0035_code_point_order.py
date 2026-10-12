"""Code-point order for every text column whose order reaches output bytes (ADR 0018 §30).

Migration 0033 made the report's order columns COLLATE "C". The sweep after it found more text
columns that an order clause (or a keyset cursor) turns into output order:

- ``items.idempotency_key``: the order of ``items.jsonl`` in the job custody package (deterministic
  /3: two downloads, and the same job on another host, must be byte-identical);
- ``evidence_objects.storage_key``: the order of the package's evidence objects (keyset pages);
- ``collection_scopes.external_id``: the order of a job's scopes in the jobs API and of the
  planning (``job_started`` lists the scopes in the caller's order and unit events index them by
  creation order, so the chain does not depend on it);
- ``items.source_item_id``: the normalizer's prior-state order;
- ``export_conversations.conversation_id`` / ``folder``, ``export_entries.folder``,
  ``export_threads.conversation_id``: the export connector's unit order and the export findings'
  capped samples (the first 100 in order: another order is another sample);
- the same column names in other tables (``items.storage_key``, ``render_natives.storage_key``,
  ``renders.idempotency_key``, ``groups.external_id``): a name the product orders by is C in every
  table, so the static check needs no alias resolution.

Why the columns and not the database default: a database's collation is fixed at CREATE DATABASE
(managed Postgres creates it ``en_US.UTF-8`` unless told otherwise, and it cannot be altered), and a
column's collation travels with dump and restore into whatever database receives it. The test
`tests/integration/db/test_collation.py` runs the corpus against a database created with a tailored
ICU collation and lists every ordered text column in the source.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0035"
down_revision: str | None = "0034"
branch_labels = None
depends_on = None

COLUMNS = (
    ("items", "idempotency_key"),
    ("items", "source_item_id"),
    ("evidence_objects", "storage_key"),
    ("collection_scopes", "external_id"),
    ("export_conversations", "conversation_id"),
    ("export_conversations", "folder"),
    ("export_entries", "folder"),
    ("export_threads", "conversation_id"),
    # the same names in other tables: one rule, "a column name the product orders by is C in every
    # table", which the static test checks without resolving table aliases
    ("items", "storage_key"),
    ("render_natives", "storage_key"),
    ("renders", "idempotency_key"),
    ("groups", "external_id"),
)


def _alter(collation: str) -> str:
    tables: dict[str, list[str]] = {}
    for table, column in COLUMNS:
        tables.setdefault(table, []).append(
            f'ALTER COLUMN {column} TYPE text COLLATE "{collation}"'
        )
    return "\n".join(f"ALTER TABLE {t} {', '.join(cols)};" for t, cols in tables.items())


def upgrade() -> None:
    for statement in split_sql(_alter("C")):
        op.execute(statement)


def downgrade() -> None:
    for statement in split_sql(_alter("default")):
        op.execute(statement)
