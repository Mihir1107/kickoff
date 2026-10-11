"""The glyph set in a report's identity (ADR 0018 §24).

`report.html` replaces every character no vendored font draws by its marker (§5.5), from the
committed coverage set `edisc_renderers/report/fonts/coverage.txt`. So the HTML's bytes depend on the
font set, and outside the report image the PDF toolchain id (which hashes the fonts) is `none`: the
identity, its unique index and the queue did not name what the HTML was built with. ``glyph_set`` is
the SHA-256 of the coverage ranges (`edisc_renderers.report.html.glyph_set_id`), part of the identity
(immutable), of the one-live-report-per-identity index and of the report queue.

Reports created before this revision built their HTML from the coverage in force then, which no
column recorded: they carry ``unrecorded`` (never equal to a computed id, so never a duplicate of a
new report).

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0032"
down_revision: str | None = "0031"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE reports ADD COLUMN glyph_set text NOT NULL DEFAULT 'unrecorded';
ALTER TABLE reports ALTER COLUMN glyph_set DROP DEFAULT;
ALTER TABLE reports ADD CONSTRAINT ck_reports_glyph_set
    CHECK (glyph_set = 'unrecorded' OR glyph_set ~ '^[0-9a-f]{64}$');
DROP INDEX uq_reports_identity;
CREATE UNIQUE INDEX uq_reports_identity ON reports
    (tenant_id, job_id, snapshot_digest, renderer_version, toolchain_id, unicode_version, glyph_set,
     paper)
    WHERE status NOT IN ('requested', 'failed', 'refused');
CREATE FUNCTION guard_reports_glyph_set() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.glyph_set IS DISTINCT FROM OLD.glyph_set THEN
        RAISE EXCEPTION 'reports: identity columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_reports_glyph_set BEFORE UPDATE ON reports FOR EACH ROW
    EXECUTE FUNCTION guard_reports_glyph_set();
GRANT SELECT (glyph_set) ON reports TO edisc_sweeper;
DROP FUNCTION stale_requested_reports(interval, integer, uuid);
CREATE FUNCTION stale_requested_reports(p_min_age interval, p_limit integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, report_id uuid, renderer_version text, toolchain_id text,
               unicode_version text, glyph_set text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT r.tenant_id, r.id, r.renderer_version, r.toolchain_id, r.unicode_version, r.glyph_set
    FROM reports r
    WHERE r.status = 'requested' AND r.created_at <= now() - p_min_age
      AND (p_tenant IS NULL OR r.tenant_id = p_tenant)
    ORDER BY r.created_at LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION stale_requested_reports(interval, integer, uuid) FROM PUBLIC;
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION stale_requested_reports(interval, integer, uuid) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""

DOWNGRADE = """
DROP FUNCTION stale_requested_reports(interval, integer, uuid);
CREATE FUNCTION stale_requested_reports(p_min_age interval, p_limit integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, report_id uuid, renderer_version text, toolchain_id text, unicode_version text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT r.tenant_id, r.id, r.renderer_version, r.toolchain_id, r.unicode_version FROM reports r
    WHERE r.status = 'requested' AND r.created_at <= now() - p_min_age
      AND (p_tenant IS NULL OR r.tenant_id = p_tenant)
    ORDER BY r.created_at LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION stale_requested_reports(interval, integer, uuid) FROM PUBLIC;
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION stale_requested_reports(interval, integer, uuid) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
REVOKE SELECT (glyph_set) ON reports FROM edisc_sweeper;
DROP TRIGGER IF EXISTS trg_reports_glyph_set ON reports;
DROP FUNCTION IF EXISTS guard_reports_glyph_set();
DROP INDEX uq_reports_identity;
CREATE UNIQUE INDEX uq_reports_identity ON reports
    (tenant_id, job_id, snapshot_digest, renderer_version, toolchain_id, unicode_version, paper)
    WHERE status NOT IN ('requested', 'failed', 'refused');
ALTER TABLE reports DROP CONSTRAINT IF EXISTS ck_reports_glyph_set;
ALTER TABLE reports DROP COLUMN IF EXISTS glyph_set;
"""


def _run(sql: str) -> None:
    for stmt in split_sql(sql):
        op.execute(stmt)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
