"""Evidence writer support (M6).

- evidence_objects.upload_id: multipart upload id recorded while a pending object is in flight, so an
  interrupted upload can be aborted explicitly (MinIO cannot list uploads by prefix).
- Guard: pending rows may record upload_id and then finalize (complete|missing) once. Complete rows are
  final except that retain_until may be EXTENDED (rolling retention, dedup reuse by another matter).

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE evidence_objects ADD COLUMN upload_id text;

CREATE OR REPLACE FUNCTION guard_evidence_objects() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.job_id, NEW.storage_key, NEW.kind, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.job_id, OLD.storage_key, OLD.kind, OLD.created_at) THEN
        RAISE EXCEPTION 'evidence_objects: identity columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.state = 'pending' THEN
        IF NEW.retain_until <> OLD.retain_until THEN
            RAISE EXCEPTION 'evidence_objects: retain_until changes only after completion' USING ERRCODE = 'EA002';
        END IF;
        IF NEW.state = 'pending' THEN
            -- only recording the in-flight multipart upload id, once
            IF OLD.upload_id IS NOT NULL OR NEW.upload_id IS NULL
               OR (NEW.sha256, NEW.size_bytes, NEW.completed_at) IS DISTINCT FROM (OLD.sha256, OLD.size_bytes, OLD.completed_at) THEN
                RAISE EXCEPTION 'evidence_objects: pending rows may only record their upload id once' USING ERRCODE = 'EA002';
            END IF;
        ELSIF NEW.state NOT IN ('complete', 'missing') THEN
            RAISE EXCEPTION 'evidence_objects: pending -> complete|missing only' USING ERRCODE = 'EA002';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'complete'
       AND NEW.state = 'complete'
       AND NEW.retain_until > OLD.retain_until
       AND (NEW.sha256, NEW.size_bytes, NEW.completed_at, NEW.upload_id)
           IS NOT DISTINCT FROM (OLD.sha256, OLD.size_bytes, OLD.completed_at, OLD.upload_id) THEN
        RETURN NEW;  -- retention extension
    END IF;
    RAISE EXCEPTION 'evidence_objects: % row % is final (only retain_until may be extended)', OLD.state, OLD.id
        USING ERRCODE = 'EA002';
END
$$;
"""

DOWNGRADE = """
CREATE OR REPLACE FUNCTION guard_evidence_objects() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state <> 'pending' THEN
        RAISE EXCEPTION 'evidence_objects: % row % is final', OLD.state, OLD.id USING ERRCODE = 'EA002';
    END IF;
    IF NEW.state NOT IN ('complete', 'missing')
       OR (NEW.id, NEW.tenant_id, NEW.job_id, NEW.storage_key, NEW.kind, NEW.retain_until, NEW.created_at)
          IS DISTINCT FROM
          (OLD.id, OLD.tenant_id, OLD.job_id, OLD.storage_key, OLD.kind, OLD.retain_until, OLD.created_at)
    THEN
        RAISE EXCEPTION 'evidence_objects: only pending -> complete|missing with hash/size is allowed'
            USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END
$$;
ALTER TABLE evidence_objects DROP COLUMN upload_id;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
