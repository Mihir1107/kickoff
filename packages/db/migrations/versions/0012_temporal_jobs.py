"""Temporal orchestration support (M12, ADR 0012 R1-R6).

- collection_jobs: statuses paused_awaiting_reauth and completed_with_failed_units; stop request
  (cancel / job failure), sealed_at, rerun_of + explicit_units (re-running failed units as a NEW job).
- Sealed jobs are closed: a trigger on items, job_items and custody_events rejects inserts for a job
  that is terminal or sealed. It takes FOR SHARE on the job row, so a batch racing finalize either
  commits before the terminal status or is rolled back.
- work_units: retry_later / paused statuses with cool-down bookkeeping.
- connections: non-secret connector config; status reauth_required.
- job_pauses (paused duration) and alerts (notification records), tenant-scoped with RLS.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None

TERMINAL = "'completed', 'completed_with_gaps', 'completed_unverified', 'completed_with_failed_units', 'failed', 'cancelled'"


def _upgrade(app_role: str) -> str:
    return f"""
ALTER TABLE collection_jobs DROP CONSTRAINT ck_collection_jobs_status;
ALTER TABLE collection_jobs ADD CONSTRAINT ck_collection_jobs_status CHECK (status IN (
    'pending', 'running', 'paused_awaiting_reauth', {TERMINAL}));
ALTER TABLE collection_jobs
    ADD COLUMN stop_requested_at timestamptz,
    ADD COLUMN stop_reason text,
    ADD COLUMN sealed_at timestamptz,
    ADD COLUMN rerun_of uuid,
    ADD COLUMN explicit_units boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT ck_collection_jobs_stop_reason CHECK (stop_reason IS NULL OR stop_reason IN ('cancel', 'job_failure')),
    ADD CONSTRAINT fk_collection_jobs_tenant_id_rerun_of_collection_jobs
        FOREIGN KEY (tenant_id, rerun_of) REFERENCES collection_jobs (tenant_id, id);

ALTER TABLE work_units DROP CONSTRAINT ck_work_units_status;
ALTER TABLE work_units ADD CONSTRAINT ck_work_units_status CHECK (
    status IN ('pending', 'running', 'done', 'failed', 'retry_later', 'paused'));
ALTER TABLE work_units
    ADD COLUMN retry_after timestamptz,
    ADD COLUMN first_failure_at timestamptz,
    ADD COLUMN failures integer NOT NULL DEFAULT 0;

ALTER TABLE connections DROP CONSTRAINT ck_connections_status;
ALTER TABLE connections ADD CONSTRAINT ck_connections_status CHECK (
    status IN ('pending', 'active', 'revoked', 'error', 'reauth_required'));
ALTER TABLE connections ADD COLUMN config jsonb NOT NULL DEFAULT '{{}}';

CREATE TABLE job_pauses (
    id             uuid        NOT NULL,
    tenant_id      uuid        NOT NULL,
    job_id         uuid        NOT NULL,
    connection_id  uuid        NOT NULL,
    reason         text        NOT NULL,
    paused_at      timestamptz NOT NULL DEFAULT now(),
    resumed_at     timestamptz,
    CONSTRAINT pk_job_pauses PRIMARY KEY (id),
    CONSTRAINT fk_job_pauses_tenant_id_job_id_collection_jobs
        FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    CONSTRAINT ck_job_pauses_order CHECK (resumed_at IS NULL OR resumed_at >= paused_at)
);
CREATE TABLE alerts (
    id               uuid        NOT NULL,
    tenant_id        uuid        NOT NULL,
    kind             text        NOT NULL,
    connection_id    uuid,
    job_id           uuid,
    message          text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    acknowledged_at  timestamptz,
    CONSTRAINT pk_alerts PRIMARY KEY (id),
    CONSTRAINT fk_alerts_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id)
);
ALTER TABLE job_pauses ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_pauses FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON job_pauses USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON alerts USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_job_pauses_no_delete BEFORE DELETE ON job_pauses FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_alerts_no_delete BEFORE DELETE ON alerts FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON job_pauses, alerts TO "{app_role}";

CREATE FUNCTION guard_job_open() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    job_status text;
    job_sealed timestamptz;
BEGIN
    IF NEW.job_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT status, sealed_at INTO job_status, job_sealed FROM collection_jobs WHERE id = NEW.job_id FOR SHARE;
    IF job_sealed IS NOT NULL OR job_status IN ({TERMINAL}) THEN
        RAISE EXCEPTION 'job % is closed (status %, sealed %): no further collection writes',
            NEW.job_id, job_status, job_sealed IS NOT NULL USING ERRCODE = 'EA005';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER trg_custody_events_job_open BEFORE INSERT ON custody_events
    FOR EACH ROW EXECUTE FUNCTION guard_job_open();
CREATE TRIGGER trg_job_items_job_open BEFORE INSERT ON job_items
    FOR EACH ROW EXECUTE FUNCTION guard_job_open();
CREATE TRIGGER trg_items_job_open BEFORE INSERT ON items
    FOR EACH ROW EXECUTE FUNCTION guard_job_open();
"""


DOWNGRADE = """
DROP TRIGGER IF EXISTS trg_items_job_open ON items;
DROP TRIGGER IF EXISTS trg_job_items_job_open ON job_items;
DROP TRIGGER IF EXISTS trg_custody_events_job_open ON custody_events;
DROP FUNCTION IF EXISTS guard_job_open();
DROP TABLE IF EXISTS alerts;
DROP TABLE IF EXISTS job_pauses;
ALTER TABLE connections DROP COLUMN config;
ALTER TABLE connections DROP CONSTRAINT ck_connections_status;
ALTER TABLE connections ADD CONSTRAINT ck_connections_status CHECK (status IN ('pending', 'active', 'revoked', 'error'));
ALTER TABLE work_units DROP COLUMN failures, DROP COLUMN first_failure_at, DROP COLUMN retry_after;
ALTER TABLE work_units DROP CONSTRAINT ck_work_units_status;
ALTER TABLE work_units ADD CONSTRAINT ck_work_units_status CHECK (status IN ('pending', 'running', 'done', 'failed'));
ALTER TABLE collection_jobs DROP CONSTRAINT fk_collection_jobs_tenant_id_rerun_of_collection_jobs,
    DROP CONSTRAINT ck_collection_jobs_stop_reason,
    DROP COLUMN explicit_units, DROP COLUMN rerun_of, DROP COLUMN sealed_at, DROP COLUMN stop_reason,
    DROP COLUMN stop_requested_at;
ALTER TABLE collection_jobs DROP CONSTRAINT ck_collection_jobs_status;
ALTER TABLE collection_jobs ADD CONSTRAINT ck_collection_jobs_status CHECK (status IN (
    'pending', 'running', 'completed', 'completed_with_gaps', 'completed_unverified', 'failed', 'cancelled'));
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
