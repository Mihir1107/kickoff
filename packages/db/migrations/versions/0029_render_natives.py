"""Oversized attachments as natives next to the render's files (ADR 0015 §11 and §20, renderer 1.3.0).

- ``render_natives``: one row per (render, SHA-256): a file kept outside the ``rsmf.zip`` and copied
  server-side into ``t/{tenant}/productions/{render}/natives/sha256/<hex>`` (evidence kind
  ``production`` with ``render_id``, so retention resolves render -> job -> matter). ``file_ords``
  lists every output file that references it. Each row is committed with the ``render_files_batch``
  event of the first file that references it (``custody_event_id``, deferred FK, like
  ``render_files``), whose ``natives_root`` covers it. Insert-only.
- ``renders.native_count`` / ``natives_root``: the totals ``render_completed`` carries (both NULL for
  renders made before 1.3.0); immutable once the render is final, like the other totals.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0029"
down_revision: str | None = "0028"
branch_labels = None
depends_on = None

HEX = "'^[0-9a-f]{64}$'"
FINAL = "('completed', 'refused', 'failed')"


def _guard(extra_old: str, extra_new: str) -> str:
    """``guard_renders`` (0026) with the final-state tuple extended by the given columns."""
    return f"""
CREATE OR REPLACE FUNCTION guard_renders() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.job_id, NEW.matter_id, NEW.options, NEW.options_hash,
        NEW.renderer_version, NEW.unicode_version, NEW.tzdata_version, NEW.requested_by,
        NEW.request_id, NEW.idempotency_key, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.job_id, OLD.matter_id, OLD.options, OLD.options_hash,
        OLD.renderer_version, OLD.unicode_version, OLD.tzdata_version, OLD.requested_by,
        OLD.request_id, OLD.idempotency_key, OLD.created_at) THEN
        RAISE EXCEPTION 'renders: identity columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.job_seal_key IS NOT NULL
       AND (NEW.job_head_seq, NEW.job_head_hash, NEW.job_seal_key, NEW.job_seal_version, NEW.started_at)
           IS DISTINCT FROM (OLD.job_head_seq, OLD.job_head_hash, OLD.job_seal_key, OLD.job_seal_version, OLD.started_at) THEN
        RAISE EXCEPTION 'renders: the job reference is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.status IN {FINAL}
       AND (NEW.status, NEW.reason, NEW.detail, NEW.batches_done, NEW.files_done, NEW.file_count,
            NEW.batches_root, NEW.summary, NEW.finished_at{extra_new})
           IS DISTINCT FROM
           (OLD.status, OLD.reason, OLD.detail, OLD.batches_done, OLD.files_done, OLD.file_count,
            OLD.batches_root, OLD.summary, OLD.finished_at{extra_old}) THEN
        RAISE EXCEPTION 'renders: % render % is final', OLD.status, OLD.id USING ERRCODE = 'EA002';
    END IF;
    IF OLD.seal_storage_key IS NOT NULL
       AND (NEW.seal_storage_key, NEW.seal_version_id, NEW.head_seq, NEW.head_hash, NEW.sealed_at)
           IS DISTINCT FROM (OLD.seal_storage_key, OLD.seal_version_id, OLD.head_seq, OLD.head_hash, OLD.sealed_at) THEN
        RAISE EXCEPTION 'renders: the seal is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN (
        ('requested', 'rendering'), ('requested', 'refused'), ('requested', 'failed'),
        ('rendering', 'rendered'), ('rendering', 'failed'),
        ('rendered', 'completed'), ('rendered', 'failed')) THEN
        RAISE EXCEPTION 'renders: % -> % is not allowed', OLD.status, NEW.status USING ERRCODE = 'EA002';
    END IF;
    IF NEW.batches_done < OLD.batches_done OR NEW.files_done < OLD.files_done THEN
        RAISE EXCEPTION 'renders: progress never goes back' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
"""


def _upgrade(app: str) -> str:
    return f"""
ALTER TABLE renders ADD COLUMN native_count bigint, ADD COLUMN natives_root text,
    ADD CONSTRAINT ck_renders_natives CHECK (
        (native_count IS NULL) = (natives_root IS NULL)
        AND (native_count IS NULL OR native_count >= 0)
        AND (natives_root IS NULL OR natives_root ~ {HEX}));
{_guard(", OLD.native_count, OLD.natives_root", ", NEW.native_count, NEW.natives_root")}

CREATE TABLE render_natives (
    tenant_id           uuid NOT NULL,
    render_id           uuid NOT NULL,
    ord                 integer NOT NULL,
    sha256              text NOT NULL,
    size_bytes          bigint NOT NULL,
    storage_key         text NOT NULL,
    version_id          text NOT NULL,
    file_ords           integer[] NOT NULL,
    evidence_object_id  uuid NOT NULL,
    custody_event_id    uuid NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_render_natives PRIMARY KEY (render_id, ord),
    CONSTRAINT uq_render_natives_render_id_sha256 UNIQUE (render_id, sha256),
    CONSTRAINT fk_render_natives_tenant_id_render_id_renders FOREIGN KEY (tenant_id, render_id) REFERENCES renders (tenant_id, id),
    CONSTRAINT fk_render_natives_tenant_id_evidence_object_id_evidence_objects FOREIGN KEY (tenant_id, evidence_object_id) REFERENCES evidence_objects (tenant_id, id),
    CONSTRAINT fk_render_natives_tenant_id_custody_event_id_custody_events FOREIGN KEY (tenant_id, custody_event_id)
        REFERENCES custody_events (tenant_id, id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_render_natives_values CHECK (
        ord >= 0 AND sha256 ~ {HEX} AND size_bytes >= 0 AND cardinality(file_ords) >= 1
        AND storage_key = 't/' || tenant_id || '/productions/' || render_id || '/natives/sha256/' || sha256)
);
CREATE INDEX ix_render_natives_custody_event_id ON render_natives (custody_event_id);
ALTER TABLE render_natives ENABLE ROW LEVEL SECURITY;
ALTER TABLE render_natives FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON render_natives USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_render_natives_no_update BEFORE UPDATE ON render_natives FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_render_natives_no_delete BEFORE DELETE ON render_natives FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON render_natives TO "{app}";
"""


DOWNGRADE = f"""
DROP TABLE IF EXISTS render_natives;
{_guard("", "")}
ALTER TABLE renders DROP CONSTRAINT IF EXISTS ck_renders_natives,
    DROP COLUMN IF EXISTS natives_root, DROP COLUMN IF EXISTS native_count;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
