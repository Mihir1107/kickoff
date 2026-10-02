"""Collecting from a locked Slack export (ADR 0014 sections 2 and 4, M14.5).

- ``evidence_objects`` kind ``archive_entry``: an entry INSIDE a locked archive, referenced, never copied.
  ``archive_evidence_id`` (the zip's registry row, whose pinned version is the entry's ``version_id``),
  ``entry_path``/``entry_raw_name`` (exact name bytes), ``entry_crc32``, ``entry_compressed_size``.
  ``sha256``/``size_bytes`` are those of the DECOMPRESSED entry, computed while reading it.
- ``work_units``: recon status ``matched_against_archive``; ``archive_accounted`` (array elements of the
  day file that became message items) and ``day_anomalies`` (elements whose own ``ts`` is not on the file's
  hinted day, R4).
- ``collection_jobs``: terminal status ``completed_against_archive`` (never clean).
- ``slack_exports.workspace_id``: the Slack team the export belongs to (from users.json).
- ``export_entries.flags``: each entry's general-purpose flags (re-checking its local header on read).
- ``export_day_files``: per day file, its element count, hint-day anomalies and parse error (validation).
- ``export_threads``: every threaded message (parent or reply) with the entry and array index holding it,
  so thread context can be fetched across day files without rereading the archive.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0022"
down_revision: str | None = "0021"
branch_labels = None
depends_on = None

TERMINAL = (
    "'completed', 'completed_with_gaps', 'completed_unverified', 'completed_with_failed_units',"
    " 'failed', 'cancelled'"
)
RECON = "'pending', 'matched', 'gap', 'surplus', 'unverifiable', 'failed', 'access_lost', 'not_applicable'"


def _upgrade(app: str) -> str:
    return f"""
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_kind;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_kind
    CHECK (kind IN ('page', 'file', 'seal', 'report', 'anchor', 'archive_entry'));
ALTER TABLE evidence_objects
    ADD COLUMN archive_evidence_id uuid,
    ADD COLUMN entry_path text,
    ADD COLUMN entry_raw_name bytea,
    ADD COLUMN entry_crc32 bigint,
    ADD COLUMN entry_compressed_size bigint,
    ADD CONSTRAINT fk_evidence_objects_tenant_id_archive_evidence_id_evidence_objects
        FOREIGN KEY (tenant_id, archive_evidence_id) REFERENCES evidence_objects (tenant_id, id),
    ADD CONSTRAINT ck_evidence_objects_archive_entry CHECK (
        (kind = 'archive_entry') = (archive_evidence_id IS NOT NULL)
        AND (kind <> 'archive_entry' OR (entry_path IS NOT NULL AND entry_raw_name IS NOT NULL
             AND entry_crc32 IS NOT NULL AND entry_compressed_size IS NOT NULL)));
CREATE OR REPLACE FUNCTION guard_archive_entry_columns() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.archive_evidence_id, NEW.entry_path, NEW.entry_raw_name, NEW.entry_crc32, NEW.entry_compressed_size)
       IS DISTINCT FROM
       (OLD.archive_evidence_id, OLD.entry_path, OLD.entry_raw_name, OLD.entry_crc32, OLD.entry_compressed_size) THEN
        RAISE EXCEPTION 'evidence_objects: archive entry references are immutable' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_evidence_objects_archive_guard BEFORE UPDATE ON evidence_objects
    FOR EACH ROW EXECUTE FUNCTION guard_archive_entry_columns();

ALTER TABLE work_units DROP CONSTRAINT ck_work_units_recon_status;
ALTER TABLE work_units ADD CONSTRAINT ck_work_units_recon_status CHECK (recon_status IN (
    {RECON}, 'matched_against_archive'));
ALTER TABLE work_units ADD COLUMN archive_accounted integer, ADD COLUMN day_anomalies integer NOT NULL DEFAULT 0,
    ADD CONSTRAINT ck_work_units_archive_counts CHECK ((archive_accounted IS NULL OR archive_accounted >= 0) AND day_anomalies >= 0);

ALTER TABLE collection_jobs DROP CONSTRAINT ck_collection_jobs_status;
ALTER TABLE collection_jobs ADD CONSTRAINT ck_collection_jobs_status CHECK (status IN (
    'pending', 'running', 'paused_awaiting_reauth', {TERMINAL}, 'completed_against_archive'));

ALTER TABLE slack_exports ADD COLUMN workspace_id text;
-- general-purpose flags of each entry (data descriptor, UTF-8): needed to re-check its local header
ALTER TABLE export_entries ADD COLUMN flags integer NOT NULL DEFAULT 0;

CREATE TABLE export_day_files (
    tenant_id    uuid NOT NULL,
    export_id    uuid NOT NULL,
    entry_idx    bigint NOT NULL,
    elements     integer,
    anomalies    integer NOT NULL DEFAULT 0,
    parse_error  text,
    CONSTRAINT pk_export_day_files PRIMARY KEY (export_id, entry_idx),
    CONSTRAINT fk_export_day_files_export_id_entry_idx_export_entries FOREIGN KEY (export_id, entry_idx) REFERENCES export_entries (export_id, idx),
    CONSTRAINT fk_export_day_files_tenant_id_export_id_slack_exports FOREIGN KEY (tenant_id, export_id) REFERENCES slack_exports (tenant_id, id),
    CONSTRAINT ck_export_day_files_parsed CHECK ((elements IS NULL) = (parse_error IS NOT NULL))
);
CREATE TABLE export_threads (
    tenant_id        uuid NOT NULL,
    export_id        uuid NOT NULL,
    entry_idx        bigint NOT NULL,
    element_idx      integer NOT NULL,
    conversation_id  text NOT NULL,
    thread_ts        text NOT NULL,
    ts               text NOT NULL,
    CONSTRAINT pk_export_threads PRIMARY KEY (export_id, entry_idx, element_idx),
    CONSTRAINT fk_export_threads_tenant_id_export_id_slack_exports FOREIGN KEY (tenant_id, export_id) REFERENCES slack_exports (tenant_id, id)
);
CREATE INDEX ix_export_threads_export_id_conversation_id_thread_ts ON export_threads (export_id, conversation_id, thread_ts);
ALTER TABLE export_threads SET (autovacuum_analyze_scale_factor = 0.02);
"""


def _tables(app: str) -> str:
    return "".join(
        f"""
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {t} USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_{t}_no_update BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON {t} TO "{app}";
"""
        for t in ("export_day_files", "export_threads")
    )


DOWNGRADE = f"""
DROP TABLE IF EXISTS export_threads;
DROP TABLE IF EXISTS export_day_files;
ALTER TABLE export_entries DROP COLUMN IF EXISTS flags;
ALTER TABLE slack_exports DROP COLUMN IF EXISTS workspace_id;
ALTER TABLE collection_jobs DROP CONSTRAINT ck_collection_jobs_status;
ALTER TABLE collection_jobs ADD CONSTRAINT ck_collection_jobs_status CHECK (status IN (
    'pending', 'running', 'paused_awaiting_reauth', {TERMINAL}));
ALTER TABLE work_units DROP CONSTRAINT ck_work_units_archive_counts, DROP COLUMN day_anomalies, DROP COLUMN archive_accounted;
ALTER TABLE work_units DROP CONSTRAINT ck_work_units_recon_status;
ALTER TABLE work_units ADD CONSTRAINT ck_work_units_recon_status CHECK (recon_status IN ({RECON}));
DROP TRIGGER IF EXISTS trg_evidence_objects_archive_guard ON evidence_objects;
DROP FUNCTION IF EXISTS guard_archive_entry_columns();
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_archive_entry,
    DROP CONSTRAINT fk_evidence_objects_tenant_id_archive_evidence_id_evidence_objects,
    DROP COLUMN entry_compressed_size, DROP COLUMN entry_crc32, DROP COLUMN entry_raw_name,
    DROP COLUMN entry_path, DROP COLUMN archive_evidence_id;
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_kind;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_kind
    CHECK (kind IN ('page', 'file', 'seal', 'report', 'anchor'));
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    app = str(op.get_context().config.attributes.get("app_role", "edisc_app"))  # type: ignore[union-attr]
    _run(_upgrade(app) + _tables(app))


def downgrade() -> None:
    _run(DOWNGRADE)
