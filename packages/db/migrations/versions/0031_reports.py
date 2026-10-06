"""Collection reports (ADR 0018 §9, §11; M16 step 4): the report's records, its custody stream, its
files as locked evidence, and production episodes generalised from render episodes.

- ``reports``: one row per report. Status ``requested -> snapshotted -> generating -> generated ->
  completed``, or ``refused`` / ``failed`` (guard trigger: transitions, identity, the write-once
  snapshot and job reference, the seal). One live report per identity (partial unique index over
  job, snapshot digest, report renderer, PDF toolchain id, Unicode, paper; failed and refused do not
  count). The PDF toolchain id is ``none`` until the PDF exists (step 3): an identity, never a NULL.
- ``report_files``: insert-only, one row per stored file (name, media type, SHA-256, size, rows,
  VersionId); ``report_generated`` lists them and its Merkle root covers them.
- ``custody_events.report_id`` (a report's own stream: stream id = report id, job_id NULL, like
  renders) and ``evidence_objects.report_id`` (report files, kind ``report``, and the report
  stream's anchors); ``source_hash_origin`` gains ``report`` (the hash of the bytes as built,
  persisted before the object exists).
- ``production_episodes``: ``render_episodes`` RENAMED and generalised (ADR 0018 §6, §9): an episode
  is about a render, a report or a job (exactly one of ``render_id``, ``report_id``, ``job_id``;
  ``subject_id`` is that id, for the one-open-per-(subject, kind) index). Every render episode keeps
  its row. New kind ``report_missing`` (a sealed job without a completed report after
  ``EDISC_REPORT_MISSING_SECONDS``, subject = the job), closed with ``report_completed``; reports
  use ``unroutable`` and ``sealing_stuck`` like renders (end reasons ``report_final`` too).
- ``sealed_jobs_without_report(limit, tenant)``, ``jobs_missing_report(min_age, limit, tenant)`` and
  ``stale_requested_reports(min_age, limit, tenant)`` (the routing check of ADR 0015 §16 for reports):
  SECURITY DEFINER, owned and executable only by the sweeper login, returning ids only, for the
  ``ensure-job-reports`` schedule (jobs of open matters only). The sweeper sees sealed jobs, open
  matters and report statuses through column grants and row policies.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0031"
down_revision: str | None = "0030"
branch_labels = None
depends_on = None

HEX = "'^[0-9a-f]{64}$'"
FINAL = "('completed', 'refused', 'failed')"


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE reports (
    id                 uuid NOT NULL,
    tenant_id          uuid NOT NULL,
    job_id             uuid NOT NULL,
    matter_id          uuid NOT NULL,
    status             text NOT NULL DEFAULT 'requested',
    reason             text,
    detail             text,
    renderer_version   text NOT NULL,
    unicode_version    text NOT NULL,
    toolchain_id       text NOT NULL,
    paper              text NOT NULL,
    requested_by       text NOT NULL,
    request_reason     text,
    request_id         text,
    snapshot           jsonb,
    snapshot_digest    text,
    job_head_seq       bigint,
    job_head_hash      text,
    job_seal_key       text,
    job_seal_version   text,
    image_digest       text,
    files_done         integer NOT NULL DEFAULT 0,
    files_root         text,
    clean              boolean,
    divergence_count   integer,
    head_seq           bigint,
    head_hash          text,
    seal_storage_key   text,
    seal_version_id    text,
    seal_failures      integer NOT NULL DEFAULT 0,
    last_seal_error    text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    snapshotted_at     timestamptz,
    started_at         timestamptz,
    finished_at        timestamptz,
    sealed_at          timestamptz,
    CONSTRAINT pk_reports PRIMARY KEY (id),
    CONSTRAINT uq_reports_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_reports_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_reports_tenant_id_job_id_collection_jobs FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    CONSTRAINT fk_reports_tenant_id_matter_id_matters FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id),
    CONSTRAINT ck_reports_status CHECK (status IN ('requested', 'snapshotted', 'generating', 'generated', 'completed', 'refused', 'failed')),
    CONSTRAINT ck_reports_reason CHECK ((status IN ('refused', 'failed')) = (reason IS NOT NULL)),
    CONSTRAINT ck_reports_paper CHECK (paper IN ('letter', 'a4')),
    CONSTRAINT ck_reports_request_reason CHECK (request_reason IS NULL OR length(request_reason) BETWEEN 1 AND 2000),
    CONSTRAINT ck_reports_hashes CHECK (
        (snapshot_digest IS NULL OR snapshot_digest ~ {HEX})
        AND (job_head_hash IS NULL OR job_head_hash ~ {HEX})
        AND (files_root IS NULL OR files_root ~ {HEX})
        AND (head_hash IS NULL OR head_hash ~ {HEX})),
    CONSTRAINT ck_reports_snapshotted CHECK (
        status IN ('requested', 'refused')
        OR (status = 'failed' AND snapshot IS NULL)
        OR (snapshot IS NOT NULL AND snapshot_digest IS NOT NULL AND snapshotted_at IS NOT NULL)),
    CONSTRAINT ck_reports_started CHECK (
        status IN ('requested', 'snapshotted', 'refused')
        OR (status = 'failed' AND job_seal_key IS NULL)
        OR (job_head_seq IS NOT NULL AND job_head_hash IS NOT NULL AND job_seal_key IS NOT NULL
            AND started_at IS NOT NULL)),
    CONSTRAINT ck_reports_generated CHECK (
        status NOT IN ('generated', 'completed')
        OR (files_root IS NOT NULL AND clean IS NOT NULL AND divergence_count IS NOT NULL)),
    CONSTRAINT ck_reports_sealed CHECK (
        (seal_storage_key IS NULL) = (sealed_at IS NULL)
        AND (seal_storage_key IS NULL OR (status IN {FINAL} AND seal_version_id IS NOT NULL
             AND head_seq IS NOT NULL AND head_hash IS NOT NULL))),
    CONSTRAINT ck_reports_counts CHECK (files_done >= 0 AND seal_failures >= 0)
);
CREATE INDEX ix_reports_tenant_id_job_id ON reports (tenant_id, job_id);
-- one live report per identity (ADR 0018 §9); a manual request with an identical identity gets the
-- existing report, a different snapshot is a new identity (earlier reports are never replaced)
CREATE UNIQUE INDEX uq_reports_identity ON reports
    (tenant_id, job_id, snapshot_digest, renderer_version, toolchain_id, unicode_version, paper)
    WHERE status NOT IN ('requested', 'failed', 'refused');

CREATE OR REPLACE FUNCTION guard_reports() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.job_id, NEW.matter_id, NEW.renderer_version,
        NEW.unicode_version, NEW.toolchain_id, NEW.paper, NEW.requested_by, NEW.request_reason,
        NEW.request_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.job_id, OLD.matter_id, OLD.renderer_version,
        OLD.unicode_version, OLD.toolchain_id, OLD.paper, OLD.requested_by, OLD.request_reason,
        OLD.request_id, OLD.created_at) THEN
        RAISE EXCEPTION 'reports: identity columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.snapshot IS NOT NULL
       AND (NEW.snapshot, NEW.snapshot_digest, NEW.snapshotted_at)
           IS DISTINCT FROM (OLD.snapshot, OLD.snapshot_digest, OLD.snapshotted_at) THEN
        RAISE EXCEPTION 'reports: the snapshot is write-once' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.job_seal_key IS NOT NULL
       AND (NEW.job_head_seq, NEW.job_head_hash, NEW.job_seal_key, NEW.job_seal_version,
            NEW.image_digest, NEW.started_at)
           IS DISTINCT FROM (OLD.job_head_seq, OLD.job_head_hash, OLD.job_seal_key,
            OLD.job_seal_version, OLD.image_digest, OLD.started_at) THEN
        RAISE EXCEPTION 'reports: the job reference is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.files_root IS NOT NULL
       AND (NEW.files_root, NEW.clean, NEW.divergence_count)
           IS DISTINCT FROM (OLD.files_root, OLD.clean, OLD.divergence_count) THEN
        RAISE EXCEPTION 'reports: the generated verdict is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.status IN {FINAL}
       AND (NEW.status, NEW.reason, NEW.detail, NEW.files_done, NEW.finished_at)
           IS DISTINCT FROM (OLD.status, OLD.reason, OLD.detail, OLD.files_done, OLD.finished_at) THEN
        RAISE EXCEPTION 'reports: % report % is final', OLD.status, OLD.id USING ERRCODE = 'EA002';
    END IF;
    IF OLD.seal_storage_key IS NOT NULL
       AND (NEW.seal_storage_key, NEW.seal_version_id, NEW.head_seq, NEW.head_hash, NEW.sealed_at)
           IS DISTINCT FROM (OLD.seal_storage_key, OLD.seal_version_id, OLD.head_seq, OLD.head_hash, OLD.sealed_at) THEN
        RAISE EXCEPTION 'reports: the seal is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN (
        ('requested', 'snapshotted'), ('requested', 'refused'), ('requested', 'failed'),
        ('snapshotted', 'generating'), ('snapshotted', 'refused'), ('snapshotted', 'failed'),
        ('generating', 'generated'), ('generating', 'failed'),
        ('generated', 'completed'), ('generated', 'failed')) THEN
        RAISE EXCEPTION 'reports: % -> % is not allowed', OLD.status, NEW.status USING ERRCODE = 'EA002';
    END IF;
    IF NEW.files_done < OLD.files_done THEN
        RAISE EXCEPTION 'reports: progress never goes back' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON reports USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_reports_guard BEFORE UPDATE ON reports FOR EACH ROW EXECUTE FUNCTION guard_reports();
CREATE TRIGGER trg_reports_no_delete BEFORE DELETE ON reports FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON reports TO "{app}";

ALTER TABLE custody_events ADD COLUMN report_id uuid,
    ADD CONSTRAINT fk_custody_events_tenant_id_report_id_reports
        FOREIGN KEY (tenant_id, report_id) REFERENCES reports (tenant_id, id),
    ADD CONSTRAINT ck_custody_events_report CHECK (
        report_id IS NULL OR (report_id = stream_id AND job_id IS NULL AND render_id IS NULL));

ALTER TABLE evidence_objects ADD COLUMN report_id uuid,
    ADD CONSTRAINT fk_evidence_objects_tenant_id_report_id_reports
        FOREIGN KEY (tenant_id, report_id) REFERENCES reports (tenant_id, id),
    ADD CONSTRAINT ck_evidence_objects_report CHECK (
        report_id IS NULL OR (kind IN ('anchor', 'report') AND render_id IS NULL));
CREATE INDEX ix_evidence_objects_report_id ON evidence_objects (report_id) WHERE report_id IS NOT NULL;
CREATE OR REPLACE FUNCTION guard_evidence_objects_report() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.report_id IS DISTINCT FROM OLD.report_id THEN
        RAISE EXCEPTION 'evidence_objects: report_id is immutable' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_evidence_objects_report_guard BEFORE UPDATE ON evidence_objects
    FOR EACH ROW EXECUTE FUNCTION guard_evidence_objects_report();
ALTER TABLE evidence_objects DROP CONSTRAINT ck_evidence_objects_source_hash;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_source_hash CHECK (
    ((source_sha256 IS NULL AND source_hash_origin IS NULL)
     OR (source_sha256 ~ '^[0-9a-f]{{64}}$'
         AND source_hash_origin IN ('collection', 'refetch', 'render', 'report')))
    AND (source_hash_origin IS DISTINCT FROM 'render' OR kind = 'production')
    AND (kind <> 'production' OR source_hash_origin IS NULL OR source_hash_origin = 'render')
    AND (source_hash_origin IS DISTINCT FROM 'report' OR kind = 'report')
    AND (kind <> 'report' OR source_hash_origin IS NULL OR source_hash_origin = 'report'));

CREATE TABLE report_files (
    tenant_id           uuid NOT NULL,
    report_id           uuid NOT NULL,
    ord                 integer NOT NULL,
    name                text NOT NULL,
    media_type          text NOT NULL,
    evidence_object_id  uuid NOT NULL,
    version_id          text NOT NULL,
    sha256              text NOT NULL,
    size_bytes          bigint NOT NULL,
    rows                bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_report_files PRIMARY KEY (report_id, ord),
    CONSTRAINT uq_report_files_report_id_name UNIQUE (report_id, name),
    CONSTRAINT fk_report_files_tenant_id_report_id_reports FOREIGN KEY (tenant_id, report_id) REFERENCES reports (tenant_id, id),
    CONSTRAINT fk_report_files_tenant_id_evidence_object_id_evidence_objects FOREIGN KEY (tenant_id, evidence_object_id) REFERENCES evidence_objects (tenant_id, id),
    CONSTRAINT ck_report_files_values CHECK (ord >= 0 AND sha256 ~ {HEX} AND size_bytes >= 0
        AND (rows IS NULL OR rows >= 0))
);
ALTER TABLE report_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_files FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_files USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_report_files_no_update BEFORE UPDATE ON report_files FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_report_files_no_delete BEFORE DELETE ON report_files FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON report_files TO "{app}";

ALTER TABLE render_episodes RENAME TO production_episodes;
ALTER TABLE production_episodes RENAME CONSTRAINT pk_render_episodes TO pk_production_episodes;
ALTER TABLE production_episodes RENAME CONSTRAINT fk_render_episodes_tenant_id_render_id_renders
    TO fk_production_episodes_tenant_id_render_id_renders;
ALTER INDEX ix_render_episodes_render_id RENAME TO ix_production_episodes_render_id;
DROP INDEX uq_render_episodes_open;
ALTER TABLE production_episodes DROP CONSTRAINT ck_render_episodes_kind,
    DROP CONSTRAINT ck_render_episodes_end,
    ALTER COLUMN render_id DROP NOT NULL,
    ADD COLUMN report_id uuid,
    ADD COLUMN job_id uuid,
    ADD COLUMN subject_id uuid;
UPDATE production_episodes SET subject_id = render_id;
ALTER TABLE production_episodes ALTER COLUMN subject_id SET NOT NULL,
    ADD CONSTRAINT fk_production_episodes_tenant_id_report_id_reports
        FOREIGN KEY (tenant_id, report_id) REFERENCES reports (tenant_id, id),
    ADD CONSTRAINT fk_production_episodes_tenant_id_job_id_collection_jobs
        FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    ADD CONSTRAINT ck_production_episodes_subject CHECK (
        num_nonnulls(render_id, report_id, job_id) = 1
        AND subject_id = coalesce(render_id, report_id, job_id)),
    ADD CONSTRAINT ck_production_episodes_kind CHECK (
        (kind IN ('unroutable', 'sealing_stuck') AND job_id IS NULL)
        OR (kind = 'report_missing' AND job_id IS NOT NULL)),
    ADD CONSTRAINT ck_production_episodes_end CHECK (
        (ended_at IS NULL) = (end_reason IS NULL)
        AND (end_reason IS NULL OR end_reason IN ('picked_up', 'worker_available', 'sealed',
             'render_final', 'report_final', 'report_completed')));
CREATE INDEX ix_production_episodes_report_id ON production_episodes (report_id) WHERE report_id IS NOT NULL;
CREATE INDEX ix_production_episodes_job_id ON production_episodes (job_id) WHERE job_id IS NOT NULL;
CREATE UNIQUE INDEX uq_production_episodes_open ON production_episodes (subject_id, kind) WHERE ended_at IS NULL;
CREATE OR REPLACE FUNCTION guard_production_episodes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.ended_at IS NOT NULL
       OR (NEW.id, NEW.tenant_id, NEW.render_id, NEW.report_id, NEW.job_id, NEW.subject_id, NEW.kind,
           NEW.started_at, NEW.detail)
          IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.render_id, OLD.report_id, OLD.job_id,
           OLD.subject_id, OLD.kind, OLD.started_at, OLD.detail) THEN
        RAISE EXCEPTION 'production_episodes: only an open episode may be closed, once' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER trg_render_episodes_guard ON production_episodes;
DROP FUNCTION guard_render_episodes();
ALTER TRIGGER trg_render_episodes_no_delete ON production_episodes RENAME TO trg_production_episodes_no_delete;
CREATE TRIGGER trg_production_episodes_guard BEFORE UPDATE ON production_episodes FOR EACH ROW
    EXECUTE FUNCTION guard_production_episodes();
GRANT SELECT (id, tenant_id, matter_id, sealed_at) ON collection_jobs TO edisc_sweeper;
CREATE POLICY sweeper_sealed ON collection_jobs FOR SELECT TO edisc_sweeper USING (sealed_at IS NOT NULL);
GRANT SELECT (id, closed_at) ON matters TO edisc_sweeper;
CREATE POLICY sweeper_open_matters ON matters FOR SELECT TO edisc_sweeper USING (closed_at IS NULL);
GRANT SELECT (id, tenant_id, job_id, status, created_at, renderer_version, toolchain_id,
    unicode_version) ON reports TO edisc_sweeper;
CREATE POLICY sweeper_reports ON reports FOR SELECT TO edisc_sweeper USING (true);
CREATE FUNCTION stale_requested_reports(p_min_age interval, p_limit integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, report_id uuid, renderer_version text, toolchain_id text, unicode_version text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT r.tenant_id, r.id, r.renderer_version, r.toolchain_id, r.unicode_version FROM reports r
    WHERE r.status = 'requested' AND r.created_at <= now() - p_min_age
      AND (p_tenant IS NULL OR r.tenant_id = p_tenant)
    ORDER BY r.created_at LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION stale_requested_reports(interval, integer, uuid) FROM PUBLIC;
CREATE FUNCTION sealed_jobs_without_report(p_limit integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, job_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT j.tenant_id, j.id FROM collection_jobs j JOIN matters m ON m.id = j.matter_id
    WHERE j.sealed_at IS NOT NULL AND m.closed_at IS NULL
      AND (p_tenant IS NULL OR j.tenant_id = p_tenant)
      AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.job_id = j.id)
    ORDER BY j.sealed_at, j.id LIMIT p_limit
$$;
CREATE FUNCTION jobs_missing_report(p_min_age interval, p_limit integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, job_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT j.tenant_id, j.id FROM collection_jobs j JOIN matters m ON m.id = j.matter_id
    WHERE j.sealed_at IS NOT NULL AND j.sealed_at <= now() - p_min_age AND m.closed_at IS NULL
      AND (p_tenant IS NULL OR j.tenant_id = p_tenant)
      AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.job_id = j.id AND r.status = 'completed')
    ORDER BY j.sealed_at, j.id LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION sealed_jobs_without_report(integer, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION jobs_missing_report(interval, integer, uuid) FROM PUBLIC;
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION sealed_jobs_without_report(integer, uuid) OWNER TO edisc_sweeper;
ALTER FUNCTION jobs_missing_report(interval, integer, uuid) OWNER TO edisc_sweeper;
ALTER FUNCTION stale_requested_reports(interval, integer, uuid) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""


DOWNGRADE = """
DROP FUNCTION IF EXISTS stale_requested_reports(interval, integer, uuid);
DROP FUNCTION IF EXISTS jobs_missing_report(interval, integer, uuid);
DROP FUNCTION IF EXISTS sealed_jobs_without_report(integer, uuid);
DROP POLICY IF EXISTS sweeper_reports ON reports;
REVOKE SELECT (id, tenant_id, job_id, status, created_at, renderer_version, toolchain_id,
    unicode_version) ON reports FROM edisc_sweeper;
DROP POLICY IF EXISTS sweeper_open_matters ON matters;
REVOKE SELECT (id, closed_at) ON matters FROM edisc_sweeper;
DROP POLICY IF EXISTS sweeper_sealed ON collection_jobs;
REVOKE SELECT (matter_id, sealed_at) ON collection_jobs FROM edisc_sweeper;
DELETE FROM production_episodes WHERE render_id IS NULL;
DROP TRIGGER IF EXISTS trg_production_episodes_guard ON production_episodes;
DROP FUNCTION IF EXISTS guard_production_episodes();
DROP INDEX IF EXISTS uq_production_episodes_open;
DROP INDEX IF EXISTS ix_production_episodes_report_id;
DROP INDEX IF EXISTS ix_production_episodes_job_id;
ALTER TABLE production_episodes DROP CONSTRAINT ck_production_episodes_subject,
    DROP CONSTRAINT ck_production_episodes_kind, DROP CONSTRAINT ck_production_episodes_end,
    DROP CONSTRAINT fk_production_episodes_tenant_id_report_id_reports,
    DROP CONSTRAINT fk_production_episodes_tenant_id_job_id_collection_jobs,
    DROP COLUMN subject_id, DROP COLUMN job_id, DROP COLUMN report_id,
    ALTER COLUMN render_id SET NOT NULL,
    ADD CONSTRAINT ck_render_episodes_kind CHECK (kind IN ('unroutable', 'sealing_stuck')),
    ADD CONSTRAINT ck_render_episodes_end CHECK (
        (ended_at IS NULL) = (end_reason IS NULL)
        AND (end_reason IS NULL OR end_reason IN ('picked_up', 'worker_available', 'sealed', 'render_final')));
ALTER TABLE production_episodes RENAME TO render_episodes;
ALTER TABLE render_episodes RENAME CONSTRAINT pk_production_episodes TO pk_render_episodes;
ALTER TABLE render_episodes RENAME CONSTRAINT fk_production_episodes_tenant_id_render_id_renders
    TO fk_render_episodes_tenant_id_render_id_renders;
ALTER INDEX ix_production_episodes_render_id RENAME TO ix_render_episodes_render_id;
CREATE UNIQUE INDEX uq_render_episodes_open ON render_episodes (render_id, kind) WHERE ended_at IS NULL;
CREATE OR REPLACE FUNCTION guard_render_episodes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.ended_at IS NOT NULL
       OR (NEW.id, NEW.tenant_id, NEW.render_id, NEW.kind, NEW.started_at, NEW.detail)
          IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.render_id, OLD.kind, OLD.started_at, OLD.detail) THEN
        RAISE EXCEPTION 'render_episodes: only an open episode may be closed, once' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TRIGGER trg_production_episodes_no_delete ON render_episodes RENAME TO trg_render_episodes_no_delete;
CREATE TRIGGER trg_render_episodes_guard BEFORE UPDATE ON render_episodes FOR EACH ROW EXECUTE FUNCTION guard_render_episodes();
DROP TABLE IF EXISTS report_files;
ALTER TABLE evidence_objects DROP CONSTRAINT IF EXISTS ck_evidence_objects_source_hash;
ALTER TABLE evidence_objects ADD CONSTRAINT ck_evidence_objects_source_hash CHECK (
    ((source_sha256 IS NULL AND source_hash_origin IS NULL)
     OR (source_sha256 ~ '^[0-9a-f]{64}$' AND source_hash_origin IN ('collection', 'refetch', 'render')))
    AND (source_hash_origin IS DISTINCT FROM 'render' OR kind = 'production')
    AND (kind <> 'production' OR source_hash_origin IS NULL OR source_hash_origin = 'render'));
DROP TRIGGER IF EXISTS trg_evidence_objects_report_guard ON evidence_objects;
DROP FUNCTION IF EXISTS guard_evidence_objects_report();
ALTER TABLE evidence_objects DROP CONSTRAINT IF EXISTS ck_evidence_objects_report,
    DROP CONSTRAINT IF EXISTS fk_evidence_objects_tenant_id_report_id_reports,
    DROP COLUMN IF EXISTS report_id;
ALTER TABLE custody_events DROP CONSTRAINT IF EXISTS ck_custody_events_report,
    DROP CONSTRAINT IF EXISTS fk_custody_events_tenant_id_report_id_reports,
    DROP COLUMN IF EXISTS report_id;
DROP TABLE IF EXISTS reports;
DROP FUNCTION IF EXISTS guard_reports();
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
