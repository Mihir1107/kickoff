"""Evidence version pinning and source-hash provenance (M6 review).

- version_id: the S3 VersionId of the object we wrote. Every read (export, verify, re-hash, review)
  goes by this id, never "latest", so a shadowing version written outside our control is never served.
- source_sha256 + source_hash_origin: the SHA-256 of the bytes as streamed FROM THE SOURCE, persisted
  while the row is pending and BEFORE the object can exist in WORM (before PutObject /
  CompleteMultipartUpload for pages, before the staging->WORM copy for files, at insert for anchors).
  origin 'collection' = computed during collection; 'refetch' = re-read from the source during recovery.
- A row can only become complete with sha256 = source_sha256 and a pinned version_id (trigger), so
  storage-derived hashes can never stand in for collection-time hashes.

Existing complete rows predate these columns; the CHECK is NOT VALID (enforced for new/changed rows).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE evidence_objects
    ADD COLUMN version_id text,
    ADD COLUMN source_sha256 text,
    ADD COLUMN source_hash_origin text,
    ADD CONSTRAINT ck_evidence_objects_source_hash CHECK (
        (source_sha256 IS NULL AND source_hash_origin IS NULL)
        OR (source_sha256 ~ '^[0-9a-f]{64}$' AND source_hash_origin IN ('collection', 'refetch')));
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_complete_pinned CHECK (
    state <> 'complete' OR (version_id IS NOT NULL AND source_sha256 IS NOT NULL AND sha256 = source_sha256)
) NOT VALID;

CREATE OR REPLACE FUNCTION guard_evidence_objects() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.job_id, NEW.storage_key, NEW.kind, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.job_id, OLD.storage_key, OLD.kind, OLD.created_at) THEN
        RAISE EXCEPTION 'evidence_objects: identity columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    -- write-once columns
    IF (OLD.upload_id IS NOT NULL AND NEW.upload_id IS DISTINCT FROM OLD.upload_id)
       OR (OLD.source_sha256 IS NOT NULL AND (NEW.source_sha256, NEW.source_hash_origin)
            IS DISTINCT FROM (OLD.source_sha256, OLD.source_hash_origin))
       OR (OLD.version_id IS NOT NULL AND NEW.version_id IS DISTINCT FROM OLD.version_id) THEN
        RAISE EXCEPTION 'evidence_objects: upload_id, source hash and version_id are write-once' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.state = 'pending' THEN
        IF NEW.retain_until <> OLD.retain_until THEN
            RAISE EXCEPTION 'evidence_objects: retain_until changes only after completion' USING ERRCODE = 'EA002';
        END IF;
        IF NEW.state = 'pending' THEN
            IF (NEW.sha256, NEW.size_bytes, NEW.completed_at, NEW.version_id)
               IS DISTINCT FROM (OLD.sha256, OLD.size_bytes, OLD.completed_at, OLD.version_id) THEN
                RAISE EXCEPTION 'evidence_objects: pending rows may only record upload id / source hash'
                    USING ERRCODE = 'EA002';
            END IF;
        ELSIF NEW.state = 'complete' THEN
            IF OLD.source_sha256 IS NULL OR NEW.sha256 IS DISTINCT FROM OLD.source_sha256 OR NEW.version_id IS NULL THEN
                RAISE EXCEPTION 'evidence_objects: completion requires the persisted source hash and a pinned version'
                    USING ERRCODE = 'EA002';
            END IF;
        ELSIF NEW.state <> 'missing' THEN
            RAISE EXCEPTION 'evidence_objects: pending -> complete|missing only' USING ERRCODE = 'EA002';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'complete' AND NEW.state = 'complete' AND NEW.retain_until > OLD.retain_until
       AND (NEW.sha256, NEW.size_bytes, NEW.completed_at)
           IS NOT DISTINCT FROM (OLD.sha256, OLD.size_bytes, OLD.completed_at) THEN
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
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_complete_pinned;
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_source_hash;
ALTER TABLE evidence_objects DROP COLUMN source_hash_origin, DROP COLUMN source_sha256, DROP COLUMN version_id;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)  # restores the 0005 guard before dropping the columns it no longer knows
