"""Slack export ingestion (ADR 0014): upload sessions, the locked archive, its directory and conversations.

- ``slack_exports``: one uploaded export. ``uploading -> locking -> validating -> ready | rejected``
  (``locking -> rejected`` on a declared-hash mismatch, R7; ``locking -> uploading`` only before anything
  is locked, when the stored parts no longer match the recorded ones). Once locked, the archive's hash, evidence
  row and pinned version never change; ``ready`` and ``rejected`` are final. ``limits`` holds the
  effective archive limits (settings plus any audited tenant-admin override, R1).
- ``export_upload_parts``: the resumable upload (part number, size, our SHA-256 of the part). A part may
  be re-sent while the export is uploading (the row is replaced); nothing is deleted.
- ``export_entries``: the central directory, written in batches by the streaming validation pass.
  ``(export_id, folded_name)`` is unique: a case- or Unicode-folded duplicate name fails the insert,
  which is how duplicates are detected without holding the directory in memory.
- ``export_conversations``: conversations listed by the metadata files (channels, groups, dms, mpims).

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0018"
down_revision: str | None = "0017"
branch_labels = None
depends_on = None

HEX = "'^[0-9a-f]{64}$'"


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE slack_exports (
    id                  uuid NOT NULL,
    tenant_id           uuid NOT NULL,
    client_id           uuid NOT NULL,
    status              text NOT NULL DEFAULT 'uploading',
    reject_reason       text,
    reject_detail       jsonb,
    declared_size       bigint NOT NULL,
    declared_sha256     text,
    declared_plan       text,
    limits              jsonb NOT NULL,
    staging_key         text NOT NULL,
    upload_id           text,
    sha256              text,
    size_bytes          bigint,
    evidence_object_id  uuid,
    version_id          text,
    entry_count         bigint,
    detected_tier       text,
    tier_confirmed      boolean,
    findings            jsonb NOT NULL DEFAULT '{{}}',
    connection_id       uuid,
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    locked_at           timestamptz,
    validated_at        timestamptz,
    CONSTRAINT pk_slack_exports PRIMARY KEY (id),
    CONSTRAINT uq_slack_exports_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_slack_exports_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_slack_exports_tenant_id_client_id_clients FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id),
    CONSTRAINT fk_slack_exports_tenant_id_evidence_object_id_evidence_objects FOREIGN KEY (tenant_id, evidence_object_id) REFERENCES evidence_objects (tenant_id, id),
    CONSTRAINT fk_slack_exports_tenant_id_connection_id_connections FOREIGN KEY (tenant_id, connection_id) REFERENCES connections (tenant_id, id),
    CONSTRAINT ck_slack_exports_status CHECK (status IN ('uploading', 'locking', 'validating', 'ready', 'rejected')),
    CONSTRAINT ck_slack_exports_reject_reason CHECK (
        (status = 'rejected') = (reject_reason IS NOT NULL)
        AND (reject_reason IS NULL OR reject_reason IN ('declared_hash_mismatch', 'archive_invalid'))),
    CONSTRAINT ck_slack_exports_declared CHECK (
        declared_size > 0
        AND (declared_sha256 IS NULL OR declared_sha256 ~ {HEX})
        AND (declared_plan IS NULL OR declared_plan IN ('free', 'pro', 'business_plus', 'enterprise_grid'))),
    CONSTRAINT ck_slack_exports_locked CHECK (
        status IN ('uploading', 'locking')
        OR (sha256 ~ {HEX} AND size_bytes >= 0 AND evidence_object_id IS NOT NULL
            AND version_id IS NOT NULL AND locked_at IS NOT NULL)),
    CONSTRAINT ck_slack_exports_ready CHECK (
        status <> 'ready'
        OR (detected_tier IS NOT NULL AND entry_count IS NOT NULL AND connection_id IS NOT NULL
            AND validated_at IS NOT NULL)),
    CONSTRAINT ck_slack_exports_tier CHECK (detected_tier IS NULL OR detected_tier IN ('public_only', 'full', 'grid'))
);
CREATE INDEX ix_slack_exports_tenant_id_client_id ON slack_exports (tenant_id, client_id);

CREATE OR REPLACE FUNCTION guard_slack_exports() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.client_id, NEW.declared_size, NEW.declared_sha256, NEW.declared_plan,
        NEW.limits, NEW.staging_key, NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.client_id, OLD.declared_size, OLD.declared_sha256, OLD.declared_plan,
        OLD.limits, OLD.staging_key, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'slack_exports: declared columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.sha256 IS NOT NULL
       AND (NEW.sha256, NEW.size_bytes, NEW.evidence_object_id, NEW.version_id, NEW.locked_at)
           IS DISTINCT FROM (OLD.sha256, OLD.size_bytes, OLD.evidence_object_id, OLD.version_id, OLD.locked_at) THEN
        RAISE EXCEPTION 'slack_exports: the locked archive is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.status IN ('ready', 'rejected') THEN
        RAISE EXCEPTION 'slack_exports: % export % is final', OLD.status, OLD.id USING ERRCODE = 'EA002';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN (
        ('uploading', 'locking'), ('locking', 'uploading'), ('locking', 'validating'), ('locking', 'rejected'),
        ('validating', 'ready'), ('validating', 'rejected')) THEN
        RAISE EXCEPTION 'slack_exports: % -> % is not allowed', OLD.status, NEW.status USING ERRCODE = 'EA002';
    END IF;
    IF NEW.status = 'uploading' AND NEW.sha256 IS NOT NULL THEN
        RAISE EXCEPTION 'slack_exports: a locked export cannot be reopened' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE slack_exports ENABLE ROW LEVEL SECURITY;
ALTER TABLE slack_exports FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON slack_exports USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_slack_exports_guard BEFORE UPDATE ON slack_exports FOR EACH ROW EXECUTE FUNCTION guard_slack_exports();
CREATE TRIGGER trg_slack_exports_no_delete BEFORE DELETE ON slack_exports FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON slack_exports TO "{app}";

CREATE TABLE export_upload_parts (
    tenant_id    uuid NOT NULL,
    export_id    uuid NOT NULL,
    part_number  integer NOT NULL,
    size_bytes   bigint NOT NULL,
    sha256       text NOT NULL,
    etag         text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_export_upload_parts PRIMARY KEY (export_id, part_number),
    CONSTRAINT fk_export_upload_parts_tenant_id_export_id_slack_exports FOREIGN KEY (tenant_id, export_id) REFERENCES slack_exports (tenant_id, id),
    CONSTRAINT ck_export_upload_parts_part CHECK (part_number BETWEEN 1 AND 10000 AND size_bytes > 0 AND sha256 ~ {HEX})
);
ALTER TABLE export_upload_parts ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_upload_parts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON export_upload_parts USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_export_upload_parts_no_delete BEFORE DELETE ON export_upload_parts FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON export_upload_parts TO "{app}";

CREATE TABLE export_entries (
    tenant_id            uuid NOT NULL,
    export_id            uuid NOT NULL,
    idx                  bigint NOT NULL,
    name                 text NOT NULL,
    folded_name          text NOT NULL,
    kind                 text NOT NULL,
    folder               text,
    hint_day             date,
    method               smallint NOT NULL,
    crc32                bigint NOT NULL,
    compressed_size      bigint NOT NULL,
    uncompressed_size    bigint NOT NULL,
    local_header_offset  bigint NOT NULL,
    CONSTRAINT pk_export_entries PRIMARY KEY (export_id, idx),
    CONSTRAINT uq_export_entries_export_id_folded_name UNIQUE (export_id, folded_name),
    CONSTRAINT fk_export_entries_tenant_id_export_id_slack_exports FOREIGN KEY (tenant_id, export_id) REFERENCES slack_exports (tenant_id, id),
    CONSTRAINT ck_export_entries_kind CHECK (kind IN ('metadata', 'day', 'directory', 'unknown')),
    CONSTRAINT ck_export_entries_day CHECK ((kind = 'day') = (hint_day IS NOT NULL AND folder IS NOT NULL))
);
CREATE INDEX ix_export_entries_export_id_kind_folder ON export_entries (export_id, kind, folder);
CREATE INDEX ix_export_entries_export_id_local_header_offset ON export_entries (export_id, local_header_offset);
ALTER TABLE export_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON export_entries USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_export_entries_no_update BEFORE UPDATE ON export_entries FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_export_entries_no_delete BEFORE DELETE ON export_entries FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON export_entries TO "{app}";
ALTER TABLE export_entries SET (autovacuum_analyze_scale_factor = 0.02);

CREATE TABLE export_conversations (
    tenant_id        uuid NOT NULL,
    export_id        uuid NOT NULL,
    conversation_id  text NOT NULL,
    kind             text NOT NULL,
    folder           text NOT NULL,
    name             text,
    metadata_entry   text NOT NULL,
    CONSTRAINT pk_export_conversations PRIMARY KEY (export_id, conversation_id),
    CONSTRAINT fk_export_conversations_tenant_id_export_id_slack_exports FOREIGN KEY (tenant_id, export_id) REFERENCES slack_exports (tenant_id, id),
    CONSTRAINT ck_export_conversations_kind CHECK (kind IN ('channel', 'group', 'dm', 'mpim'))
);
CREATE INDEX ix_export_conversations_export_id_folder ON export_conversations (export_id, folder);
ALTER TABLE export_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_conversations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON export_conversations USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_export_conversations_no_update BEFORE UPDATE ON export_conversations FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_export_conversations_no_delete BEFORE DELETE ON export_conversations FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON export_conversations TO "{app}";
"""


DOWNGRADE = """
DROP TABLE IF EXISTS export_conversations;
DROP TABLE IF EXISTS export_entries;
DROP TABLE IF EXISTS export_upload_parts;
DROP TABLE IF EXISTS slack_exports;
DROP FUNCTION IF EXISTS guard_slack_exports();
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
