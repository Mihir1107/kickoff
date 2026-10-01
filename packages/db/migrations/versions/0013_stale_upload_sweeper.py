"""Stale-upload sweeper support (ADR 0012 section 7): list open jobs with abandoned pending evidence.

- ``stale_pending_evidence(min_age, limit)`` is SECURITY DEFINER, owned and executable only by the
  sweeper login, and returns ids only (tenant_id, job_id). Recovery itself runs as the app role per
  tenant through ``tenant_tx`` (RLS applies).
- Only jobs that have not finished: finalize already recovers a job's evidence, and a sealed job is
  closed to new custody events.
- The sweeper sees a few columns of pending evidence rows and of unfinished jobs, nothing else.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None

UPGRADE = """
GRANT SELECT (tenant_id, job_id, state, created_at) ON evidence_objects TO edisc_sweeper;
CREATE POLICY sweeper_pending ON evidence_objects FOR SELECT TO edisc_sweeper USING (state = 'pending');
GRANT SELECT (id, tenant_id, finished_at) ON collection_jobs TO edisc_sweeper;
CREATE POLICY sweeper_unfinished ON collection_jobs FOR SELECT TO edisc_sweeper USING (finished_at IS NULL);
CREATE FUNCTION stale_pending_evidence(p_min_age interval, p_limit integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, job_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT DISTINCT e.tenant_id, e.job_id FROM evidence_objects e
    JOIN collection_jobs j ON j.tenant_id = e.tenant_id AND j.id = e.job_id
    WHERE e.state = 'pending' AND e.created_at < now() - p_min_age AND j.finished_at IS NULL
      AND (p_tenant IS NULL OR e.tenant_id = p_tenant)
    LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION stale_pending_evidence(interval, integer, uuid) FROM PUBLIC;
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION stale_pending_evidence(interval, integer, uuid) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""

DOWNGRADE = """
DROP FUNCTION IF EXISTS stale_pending_evidence(interval, integer, uuid);
DROP POLICY IF EXISTS sweeper_unfinished ON collection_jobs;
DROP POLICY IF EXISTS sweeper_pending ON evidence_objects;
REVOKE SELECT (id, tenant_id, finished_at) ON collection_jobs FROM edisc_sweeper;
REVOKE SELECT (tenant_id, job_id, state, created_at) ON evidence_objects FROM edisc_sweeper;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
