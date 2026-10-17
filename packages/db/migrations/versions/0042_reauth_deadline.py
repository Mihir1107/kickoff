"""A paused job ends with its access lost, never open-ended (M17 step 8; ADR 0016 A6, A2.1, A2.2).

- ``tenants.reauth_deadline_hours``: how long a job paused for re-authorization waits, a tenant
  setting bounded 24 h .. 90 days (2,160 h), default 7 days. The app may update this column only
  (column privilege; a tenant admin through the API, audited).
- ``job_pauses``: each pause records its reason CLASS (``credentials_revoked``,
  ``credentials_expired``, ``consent_withdrawn``, ``platform_credential_expired``), its deadline
  and warning time (fixed when it starts, from the tenant's setting then), when the warning alert
  was raised, and how it ENDED: ``reauthorized`` (the job resumed), ``deadline`` or
  ``finished_without_access`` (every unit not yet reconciled closes ``access_lost``), with the actor
  and, for the explicit action, the reason given. ``resumed_at`` stays "the pause ended at".
  The end is final (trigger).
- ``alerts.resolved_at``: closing a pause resolves its alerts (``job_paused``,
  ``reauth_deadline_near``).
- ``collection_jobs.stop_reason`` gains ``access_lost``: the job finalizes once every unit is
  settled (``completed_with_gaps``).
- ``connections.credentials_checked_at``: the last ``check-connections`` call that reached the
  source with this connection's credentials.
- ``open_reauth_pauses(limit)``: the sweeper's cross-tenant view (ids and times only) of pauses that
  need work: a decided end not yet carried out, a deadline passed, a warning due.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0042"
down_revision: str | None = "0041"
branch_labels = None
depends_on = None

CLASSES = (
    "('credentials_revoked', 'credentials_expired', 'consent_withdrawn',"
    " 'platform_credential_expired')"
)


def _upgrade(app: str) -> str:
    return f"""
ALTER TABLE tenants ADD COLUMN reauth_deadline_hours integer NOT NULL DEFAULT 168;
ALTER TABLE tenants ADD CONSTRAINT ck_tenants_reauth_deadline_hours
    CHECK (reauth_deadline_hours BETWEEN 24 AND 2160);
GRANT UPDATE (reauth_deadline_hours) ON tenants TO "{app}";

ALTER TABLE job_pauses
    ADD COLUMN reason_class text,
    ADD COLUMN deadline_at timestamptz,
    ADD COLUMN warn_at timestamptz,
    ADD COLUMN warned_at timestamptz,
    ADD COLUMN ended_how text,
    ADD COLUMN ended_by text,
    ADD COLUMN end_reason text,
    ADD CONSTRAINT ck_job_pauses_reason_class CHECK (reason_class IS NULL OR reason_class IN {CLASSES}),
    ADD CONSTRAINT ck_job_pauses_ended_how CHECK (ended_how IS NULL
        OR ended_how IN ('reauthorized', 'deadline', 'finished_without_access')),
    ADD CONSTRAINT ck_job_pauses_end_reason CHECK (end_reason IS NULL OR length(end_reason) <= 2000),
    ADD CONSTRAINT ck_job_pauses_deadline CHECK (deadline_at IS NULL
        OR (warn_at IS NOT NULL AND warn_at <= deadline_at AND deadline_at > paused_at));

CREATE FUNCTION guard_job_pauses() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.job_id, NEW.connection_id, NEW.paused_at, NEW.reason_class,
        NEW.deadline_at, NEW.warn_at)
       IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.job_id, OLD.connection_id, OLD.paused_at,
        OLD.reason_class, OLD.deadline_at, OLD.warn_at) THEN
        RAISE EXCEPTION 'job_pauses: a pause''s identity and deadline are fixed' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.ended_how IS NOT NULL AND (NEW.ended_how, NEW.ended_by, NEW.end_reason)
       IS DISTINCT FROM (OLD.ended_how, OLD.ended_by, OLD.end_reason) THEN
        RAISE EXCEPTION 'job_pauses: how a pause ended is final' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.resumed_at IS NOT NULL AND NEW.resumed_at IS DISTINCT FROM OLD.resumed_at THEN
        RAISE EXCEPTION 'job_pauses: the end of a pause is final' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.warned_at IS NOT NULL AND NEW.warned_at IS DISTINCT FROM OLD.warned_at THEN
        RAISE EXCEPTION 'job_pauses: a pause is warned once' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER trg_job_pauses_guard BEFORE UPDATE ON job_pauses
    FOR EACH ROW EXECUTE FUNCTION guard_job_pauses();
CREATE INDEX ix_job_pauses_open ON job_pauses (deadline_at) WHERE resumed_at IS NULL;

ALTER TABLE alerts ADD COLUMN resolved_at timestamptz;

ALTER TABLE collection_jobs DROP CONSTRAINT ck_collection_jobs_stop_reason;
ALTER TABLE collection_jobs ADD CONSTRAINT ck_collection_jobs_stop_reason
    CHECK (stop_reason IS NULL OR stop_reason IN ('cancel', 'job_failure', 'access_lost'));

ALTER TABLE connections ADD COLUMN credentials_checked_at timestamptz;

-- the sweeper's view: ids and times of pauses that need work, nothing else
CREATE FUNCTION open_reauth_pauses(p_limit integer)
RETURNS TABLE (tenant_id uuid, job_id uuid, pause_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT p.tenant_id, p.job_id, p.id FROM job_pauses p
    WHERE p.resumed_at IS NULL AND (p.ended_how IS NOT NULL OR p.deadline_at <= now()
        OR (p.warned_at IS NULL AND p.warn_at <= now()))
    ORDER BY p.deadline_at NULLS LAST, p.id LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION open_reauth_pauses(integer) FROM PUBLIC;
GRANT SELECT (id, tenant_id, job_id, deadline_at, warn_at, warned_at, ended_how, resumed_at)
    ON job_pauses TO edisc_sweeper;
CREATE POLICY sweeper_open_pauses ON job_pauses FOR SELECT TO edisc_sweeper USING (resumed_at IS NULL);

-- the connections check-connections calls: active, holding credentials (ids and source only)
CREATE FUNCTION connections_to_check(p_limit integer)
RETURNS TABLE (tenant_id uuid, connection_id uuid, source text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT c.tenant_id, c.id, c.source FROM connections c
    WHERE c.status = 'active' AND c.token_version > 0
    ORDER BY c.credentials_checked_at NULLS FIRST, c.id LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION connections_to_check(integer) FROM PUBLIC;
GRANT SELECT (id, tenant_id, source, status, token_version, credentials_checked_at)
    ON connections TO edisc_sweeper;
CREATE POLICY sweeper_check_connections ON connections FOR SELECT TO edisc_sweeper USING (true);

GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION open_reauth_pauses(integer) OWNER TO edisc_sweeper;
ALTER FUNCTION connections_to_check(integer) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""


def _downgrade(app: str) -> str:
    return f"""
DROP FUNCTION IF EXISTS connections_to_check(integer);
DROP POLICY IF EXISTS sweeper_check_connections ON connections;
REVOKE SELECT (id, tenant_id, source, status, token_version, credentials_checked_at)
    ON connections FROM edisc_sweeper;
DROP FUNCTION IF EXISTS open_reauth_pauses(integer);
DROP POLICY IF EXISTS sweeper_open_pauses ON job_pauses;
REVOKE SELECT (id, tenant_id, job_id, deadline_at, warn_at, warned_at, ended_how, resumed_at)
    ON job_pauses FROM edisc_sweeper;
ALTER TABLE connections DROP COLUMN IF EXISTS credentials_checked_at;
ALTER TABLE collection_jobs DROP CONSTRAINT ck_collection_jobs_stop_reason;
ALTER TABLE collection_jobs ADD CONSTRAINT ck_collection_jobs_stop_reason
    CHECK (stop_reason IS NULL OR stop_reason IN ('cancel', 'job_failure'));
ALTER TABLE alerts DROP COLUMN IF EXISTS resolved_at;
DROP INDEX IF EXISTS ix_job_pauses_open;
DROP TRIGGER IF EXISTS trg_job_pauses_guard ON job_pauses;
DROP FUNCTION IF EXISTS guard_job_pauses();
ALTER TABLE job_pauses DROP CONSTRAINT IF EXISTS ck_job_pauses_deadline,
    DROP CONSTRAINT IF EXISTS ck_job_pauses_end_reason, DROP CONSTRAINT IF EXISTS ck_job_pauses_ended_how,
    DROP CONSTRAINT IF EXISTS ck_job_pauses_reason_class, DROP COLUMN IF EXISTS end_reason,
    DROP COLUMN IF EXISTS ended_by, DROP COLUMN IF EXISTS ended_how, DROP COLUMN IF EXISTS warned_at,
    DROP COLUMN IF EXISTS warn_at, DROP COLUMN IF EXISTS deadline_at, DROP COLUMN IF EXISTS reason_class;
REVOKE UPDATE (reauth_deadline_hours) ON tenants FROM "{app}";
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_reauth_deadline_hours;
ALTER TABLE tenants DROP COLUMN IF EXISTS reauth_deadline_hours;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def _app() -> str:
    return str(op.get_context().config.attributes.get("app_role", "edisc_app"))  # type: ignore[union-attr]


def upgrade() -> None:
    _run(_upgrade(_app()))


def downgrade() -> None:
    _run(_downgrade(_app()))
