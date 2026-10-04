"""RSMF render outputs as registry rows (ADR 0015 §7, M15 step 3).

- ``evidence_objects.kind`` gains ``production``. A render's files are written under
  ``t/{tenant}/productions/{render}/...``, locked like evidence and tied to the RENDERED job, so the
  job's matter owns their retention (the retention extension job already covers rows of a matter's jobs).
- ``source_hash_origin`` gains ``render``: the SHA-256 of the bytes as the renderer produced them,
  persisted on the pending row BEFORE the object can exist in WORM (the same provenance rule as pages).

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0024"
down_revision: str | None = "0023"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_kind;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_kind
    CHECK (kind IN ('page', 'file', 'seal', 'report', 'anchor', 'archive_entry', 'production'));
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_source_hash;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_source_hash CHECK (
    ((source_sha256 IS NULL AND source_hash_origin IS NULL)
     OR (source_sha256 ~ '^[0-9a-f]{64}$' AND source_hash_origin IN ('collection', 'refetch', 'render')))
    -- 'render' is the origin of productions only, and productions have no other origin
    AND (source_hash_origin IS DISTINCT FROM 'render' OR kind = 'production')
    AND (kind <> 'production' OR source_hash_origin IS NULL OR source_hash_origin = 'render'));
"""

DOWNGRADE = """
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_source_hash;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_source_hash CHECK (
    (source_sha256 IS NULL AND source_hash_origin IS NULL)
    OR (source_sha256 ~ '^[0-9a-f]{64}$' AND source_hash_origin IN ('collection', 'refetch')));
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_kind;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_kind
    CHECK (kind IN ('page', 'file', 'seal', 'report', 'anchor', 'archive_entry'));
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
