"""RSMF renders as first-class records (ADR 0015 §7 and §14, M15 step 4).

- ``renders``: one render of a sealed job. Identity = (job, options hash, renderer, Unicode and
  tzdata versions); at most one render per identity that is not ``failed`` or ``refused`` (the API
  returns it instead of creating another). Status ``requested -> rendering -> rendered -> completed``,
  or ``refused`` (before anything is rendered) / ``failed``. Final states never change, except that the
  seal (key, version, final head) is recorded once afterwards. The reference to the sealed job (its
  head and seal anchor) is recorded with ``render_started``.
- ``render_files``: every stored output file, in render order. Each row is committed with the
  ``render_files_batch`` custody event whose Merkle root covers it (``custody_event_id``, deferred FK,
  like ``job_items``). Insert-only.
- ``custody_events.render_id``: set on every event of a render's stream. Render events keep
  ``job_id`` NULL (a sealed job's chain is never appended to, and ``guard_job_open`` would refuse it);
  the column equals the stream id, which the event hash covers.
- ``evidence_objects.render_id``: set on a render's productions and on its stream's anchor rows, so
  retention resolves render -> job -> matter.
- ``api_idempotency.render_id``: an Idempotency-Key used to create a render.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0026"
down_revision: str | None = "0025"
branch_labels = None
depends_on = None

HEX = "'^[0-9a-f]{64}$'"
FINAL = "('completed', 'refused', 'failed')"


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE renders (
    id                 uuid NOT NULL,
    tenant_id          uuid NOT NULL,
    job_id             uuid NOT NULL,
    matter_id          uuid NOT NULL,
    options            jsonb NOT NULL,
    options_hash       text NOT NULL,
    renderer_version   text NOT NULL,
    unicode_version    text NOT NULL,
    tzdata_version     text NOT NULL,
    status             text NOT NULL DEFAULT 'requested',
    reason             text,
    detail             text,
    requested_by       text NOT NULL,
    request_id         text,
    idempotency_key    text,
    job_head_seq       bigint,
    job_head_hash      text,
    job_seal_key       text,
    job_seal_version   text,
    batches_done       integer NOT NULL DEFAULT 0,
    files_done         bigint NOT NULL DEFAULT 0,
    file_count         bigint,
    batches_root       text,
    summary            jsonb,
    head_seq           bigint,
    head_hash          text,
    seal_storage_key   text,
    seal_version_id    text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    started_at         timestamptz,
    finished_at        timestamptz,
    sealed_at          timestamptz,
    CONSTRAINT pk_renders PRIMARY KEY (id),
    CONSTRAINT uq_renders_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_renders_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_renders_tenant_id_job_id_collection_jobs FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    CONSTRAINT fk_renders_tenant_id_matter_id_matters FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id),
    CONSTRAINT ck_renders_status CHECK (status IN ('requested', 'rendering', 'rendered', 'completed', 'refused', 'failed')),
    CONSTRAINT ck_renders_reason CHECK ((status IN ('refused', 'failed')) = (reason IS NOT NULL)),
    CONSTRAINT ck_renders_hashes CHECK (
        options_hash ~ {HEX}
        AND (job_head_hash IS NULL OR job_head_hash ~ {HEX})
        AND (batches_root IS NULL OR batches_root ~ {HEX})
        AND (head_hash IS NULL OR head_hash ~ {HEX})),
    CONSTRAINT ck_renders_started CHECK (
        status IN ('requested', 'refused')
        OR (status = 'failed' AND job_seal_key IS NULL)
        OR (job_head_seq IS NOT NULL AND job_head_hash IS NOT NULL AND job_seal_key IS NOT NULL
            AND job_seal_version IS NOT NULL AND started_at IS NOT NULL)),
    CONSTRAINT ck_renders_rendered CHECK (
        status NOT IN ('rendered', 'completed')
        OR (file_count IS NOT NULL AND file_count = files_done AND batches_root IS NOT NULL
            AND summary IS NOT NULL)),
    CONSTRAINT ck_renders_sealed CHECK (
        (seal_storage_key IS NULL) = (sealed_at IS NULL)
        AND (seal_storage_key IS NULL OR (status IN {FINAL} AND seal_version_id IS NOT NULL
             AND head_seq IS NOT NULL AND head_hash IS NOT NULL))),
    CONSTRAINT ck_renders_counts CHECK (batches_done >= 0 AND files_done >= 0)
);
CREATE INDEX ix_renders_tenant_id_job_id ON renders (tenant_id, job_id);
-- one live render per identity: failed and refused renders do not count (ADR 0015 §14)
CREATE UNIQUE INDEX uq_renders_identity ON renders
    (tenant_id, job_id, options_hash, renderer_version, unicode_version, tzdata_version)
    WHERE status NOT IN ('failed', 'refused');

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
            NEW.batches_root, NEW.summary, NEW.finished_at)
           IS DISTINCT FROM
           (OLD.status, OLD.reason, OLD.detail, OLD.batches_done, OLD.files_done, OLD.file_count,
            OLD.batches_root, OLD.summary, OLD.finished_at) THEN
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
ALTER TABLE renders ENABLE ROW LEVEL SECURITY;
ALTER TABLE renders FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON renders USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_renders_guard BEFORE UPDATE ON renders FOR EACH ROW EXECUTE FUNCTION guard_renders();
CREATE TRIGGER trg_renders_no_delete BEFORE DELETE ON renders FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON renders TO "{app}";

ALTER TABLE custody_events ADD COLUMN render_id uuid,
    ADD CONSTRAINT fk_custody_events_tenant_id_render_id_renders
        FOREIGN KEY (tenant_id, render_id) REFERENCES renders (tenant_id, id),
    ADD CONSTRAINT ck_custody_events_render CHECK (
        render_id IS NULL OR (render_id = stream_id AND job_id IS NULL));

CREATE TABLE render_files (
    tenant_id           uuid NOT NULL,
    render_id           uuid NOT NULL,
    ord                 integer NOT NULL,
    name                text NOT NULL,
    evidence_object_id  uuid NOT NULL,
    version_id          text NOT NULL,
    sha256              text NOT NULL,
    size_bytes          bigint NOT NULL,
    record              jsonb NOT NULL,
    custody_event_id    uuid NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_render_files PRIMARY KEY (render_id, ord),
    CONSTRAINT uq_render_files_render_id_name UNIQUE (render_id, name),
    CONSTRAINT fk_render_files_tenant_id_render_id_renders FOREIGN KEY (tenant_id, render_id) REFERENCES renders (tenant_id, id),
    CONSTRAINT fk_render_files_tenant_id_evidence_object_id_evidence_objects FOREIGN KEY (tenant_id, evidence_object_id) REFERENCES evidence_objects (tenant_id, id),
    CONSTRAINT fk_render_files_tenant_id_custody_event_id_custody_events FOREIGN KEY (tenant_id, custody_event_id)
        REFERENCES custody_events (tenant_id, id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_render_files_values CHECK (ord >= 0 AND sha256 ~ {HEX} AND size_bytes >= 0)
);
CREATE INDEX ix_render_files_custody_event_id ON render_files (custody_event_id);
ALTER TABLE render_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE render_files FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON render_files USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_render_files_no_update BEFORE UPDATE ON render_files FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_render_files_no_delete BEFORE DELETE ON render_files FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON render_files TO "{app}";

ALTER TABLE evidence_objects ADD COLUMN render_id uuid,
    ADD CONSTRAINT fk_evidence_objects_tenant_id_render_id_renders
        FOREIGN KEY (tenant_id, render_id) REFERENCES renders (tenant_id, id),
    ADD CONSTRAINT ck_evidence_objects_render CHECK (
        render_id IS NULL OR kind IN ('anchor', 'production'));
CREATE INDEX ix_evidence_objects_render_id ON evidence_objects (render_id) WHERE render_id IS NOT NULL;
CREATE OR REPLACE FUNCTION guard_evidence_objects_render() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.render_id IS DISTINCT FROM OLD.render_id THEN
        RAISE EXCEPTION 'evidence_objects: render_id is immutable' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_evidence_objects_render_guard BEFORE UPDATE ON evidence_objects
    FOR EACH ROW EXECUTE FUNCTION guard_evidence_objects_render();

ALTER TABLE api_idempotency ADD COLUMN render_id uuid,
    ADD CONSTRAINT fk_api_idempotency_tenant_id_render_id_renders
        FOREIGN KEY (tenant_id, render_id) REFERENCES renders (tenant_id, id),
    ADD CONSTRAINT ck_api_idempotency_target CHECK (job_id IS NULL OR render_id IS NULL);
"""


DOWNGRADE = """
ALTER TABLE api_idempotency DROP CONSTRAINT IF EXISTS ck_api_idempotency_target,
    DROP CONSTRAINT IF EXISTS fk_api_idempotency_tenant_id_render_id_renders, DROP COLUMN IF EXISTS render_id;
DROP TRIGGER IF EXISTS trg_evidence_objects_render_guard ON evidence_objects;
DROP FUNCTION IF EXISTS guard_evidence_objects_render();
DROP INDEX IF EXISTS ix_evidence_objects_render_id;
ALTER TABLE evidence_objects DROP CONSTRAINT IF EXISTS ck_evidence_objects_render,
    DROP CONSTRAINT IF EXISTS fk_evidence_objects_tenant_id_render_id_renders, DROP COLUMN IF EXISTS render_id;
DROP TABLE IF EXISTS render_files;
ALTER TABLE custody_events DROP CONSTRAINT IF EXISTS ck_custody_events_render,
    DROP CONSTRAINT IF EXISTS fk_custody_events_tenant_id_render_id_renders, DROP COLUMN IF EXISTS render_id;
DROP TABLE IF EXISTS renders;
DROP FUNCTION IF EXISTS guard_renders();
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
