"""Directory snapshots for the scope picker (M17 plan §3.3; ADR 0016 A2.7 D14, A2.8 D12).

- ``directory_snapshots``: one GENERATION of a connection's directory (its conversations and its
  users), captured in the background by ``refresh-directories`` (never on a request), page by page
  through the rate limiter. ``source_cursor`` is the checkpoint: the cursor of the next source page,
  written in the transaction that stores the page's entries, so a SIGKILL resumes exactly there.
  Status: ``capturing`` (one per connection at most), then ``complete`` (served), ``failed``
  (never served), and ``superseded`` (a complete generation older than the current and the previous
  one: kept, per the no-delete rule; paging that started in it continues). The status only moves
  forward and a generation's identity is fixed (trigger).
- ``directory_entries``: the entries of one generation, insert-only. One row per (kind, external
  id) and generation: a page stored twice (a retry after a crash) or an entity listed on two pages
  adds nothing. ``external_id`` and ``search_key`` are COLLATE "C" (code-point order whatever the
  database's collation, ADR 0018 §30); the search index serves keyset pages and prefix queries.
- ``directories_due(max_age_seconds, limit)``: the sweeper's cross-tenant view (ids and source
  only) of active connections whose directory needs work: a generation still capturing, none
  complete yet, the current one older than ``max_age_seconds``, or credentials replaced since it
  started (a re-authorization).

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0043"
down_revision: str | None = "0042"
branch_labels = None
depends_on = None


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE directory_snapshots (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    generation integer NOT NULL,
    status text NOT NULL DEFAULT 'capturing',
    connector_version text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    failed_reason text,
    source_cursor text,
    pages integer NOT NULL DEFAULT 0,
    conversations integer NOT NULL DEFAULT 0,
    users integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_directory_snapshots PRIMARY KEY (id),
    CONSTRAINT uq_directory_snapshots_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_directory_snapshots_connection_id_generation UNIQUE (connection_id, generation),
    CONSTRAINT fk_directory_snapshots_tenant_id_connection_id_connections
        FOREIGN KEY (tenant_id, connection_id) REFERENCES connections (tenant_id, id),
    CONSTRAINT ck_directory_snapshots_status
        CHECK (status IN ('capturing', 'complete', 'failed', 'superseded')),
    CONSTRAINT ck_directory_snapshots_completed
        CHECK ((completed_at IS NOT NULL) = (status IN ('complete', 'superseded'))),
    CONSTRAINT ck_directory_snapshots_failed
        CHECK ((failed_reason IS NOT NULL) = (status = 'failed')),
    CONSTRAINT ck_directory_snapshots_failed_reason CHECK (length(failed_reason) <= 2000),
    CONSTRAINT ck_directory_snapshots_generation CHECK (generation > 0)
);
CREATE UNIQUE INDEX uq_directory_snapshots_capturing ON directory_snapshots (connection_id)
    WHERE status = 'capturing';
CREATE INDEX ix_directory_snapshots_complete ON directory_snapshots (connection_id, generation DESC)
    WHERE status = 'complete';

CREATE FUNCTION guard_directory_snapshots() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.connection_id, NEW.generation, NEW.connector_version,
        NEW.started_at)
       IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.connection_id, OLD.generation,
        OLD.connector_version, OLD.started_at) THEN
        RAISE EXCEPTION 'directory_snapshots: a generation''s identity is fixed' USING ERRCODE = 'EA002';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'capturing' AND NEW.status IN ('complete', 'failed'))
        OR (OLD.status = 'complete' AND NEW.status = 'superseded')) THEN
        RAISE EXCEPTION 'directory_snapshots: status % cannot become %', OLD.status, NEW.status
            USING ERRCODE = 'EA002';
    END IF;
    IF OLD.status <> 'capturing' AND (NEW.source_cursor, NEW.pages, NEW.conversations, NEW.users)
       IS DISTINCT FROM (OLD.source_cursor, OLD.pages, OLD.conversations, OLD.users) THEN
        RAISE EXCEPTION 'directory_snapshots: a finished generation does not change'
            USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER trg_directory_snapshots_guard BEFORE UPDATE ON directory_snapshots
    FOR EACH ROW EXECUTE FUNCTION guard_directory_snapshots();

CREATE TABLE directory_entries (
    tenant_id uuid NOT NULL,
    snapshot_id uuid NOT NULL,
    kind text NOT NULL,
    external_id text COLLATE "C" NOT NULL,
    name text,
    name_units integer[],
    entry_type text NOT NULL,
    archived boolean NOT NULL,
    search_key text COLLATE "C" NOT NULL,
    CONSTRAINT pk_directory_entries PRIMARY KEY (snapshot_id, kind, external_id),
    CONSTRAINT fk_directory_entries_tenant_id_snapshot_id_directory_snapshots
        FOREIGN KEY (tenant_id, snapshot_id) REFERENCES directory_snapshots (tenant_id, id),
    CONSTRAINT ck_directory_entries_kind CHECK (kind IN ('conversation', 'user')),
    CONSTRAINT ck_directory_entries_name CHECK (length(name) <= 1024),
    CONSTRAINT ck_directory_entries_units CHECK (name_units IS NULL OR name IS NOT NULL)
);
CREATE INDEX ix_directory_entries_search
    ON directory_entries (snapshot_id, kind, search_key, external_id);
ALTER TABLE directory_entries SET (autovacuum_analyze_scale_factor = 0.02);

ALTER TABLE directory_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE directory_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON directory_snapshots USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());
ALTER TABLE directory_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE directory_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON directory_entries USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());
GRANT SELECT, INSERT, UPDATE ON directory_snapshots TO "{app}";
GRANT SELECT, INSERT ON directory_entries TO "{app}";

-- the sweeper's view: which connections' directories need work (ids and source only)
CREATE FUNCTION directories_due(p_max_age_seconds integer, p_limit integer)
RETURNS TABLE (tenant_id uuid, connection_id uuid, source text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT c.tenant_id, c.id, c.source FROM connections c
    LEFT JOIN LATERAL (
        SELECT d.status, d.started_at FROM directory_snapshots d
        WHERE d.connection_id = c.id AND d.status IN ('capturing', 'complete')
        ORDER BY d.generation DESC LIMIT 1
    ) last ON true
    WHERE c.status = 'active' AND (last.status IS NULL OR last.status = 'capturing'
        OR last.started_at < now() - make_interval(secs => p_max_age_seconds)
        OR c.token_updated_at > last.started_at)
    ORDER BY (last.status = 'capturing') DESC NULLS FIRST, last.started_at NULLS FIRST, c.id
    LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION directories_due(integer, integer) FROM PUBLIC;
GRANT SELECT (id, connection_id, status, started_at, generation) ON directory_snapshots
    TO edisc_sweeper;
CREATE POLICY sweeper_directories_due ON directory_snapshots FOR SELECT TO edisc_sweeper
    USING (status IN ('capturing', 'complete'));
GRANT SELECT (token_updated_at) ON connections TO edisc_sweeper;

GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION directories_due(integer, integer) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""


def _downgrade(app: str) -> str:
    return """
DROP FUNCTION IF EXISTS directories_due(integer, integer);
REVOKE SELECT (token_updated_at) ON connections FROM edisc_sweeper;
DROP TABLE IF EXISTS directory_entries;
DROP TABLE IF EXISTS directory_snapshots;
DROP FUNCTION IF EXISTS guard_directory_snapshots();
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
