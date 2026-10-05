"""Render episodes (ADR 0015 §16): unroutable renders and stuck sealing as episodes with history.

- ``render_episodes``: one row per episode of a condition on a render, ``kind`` ``unroutable`` (the
  render waits in ``requested`` and no worker polls the queue of its versions) or ``sealing_stuck``
  (the seal keeps failing). At most one OPEN episode per render and kind (partial unique index), so a
  condition raises one alert per episode; a closed episode is history and never changes again. The
  row carries why it ended (``picked_up``, ``worker_available``, ``sealed``, ``render_final``).
- ``renders.sealing_stuck_at`` (migration 0027) is replaced by ``sealing_stuck`` episodes.
- ``stale_requested_renders(min_age, limit)``: SECURITY DEFINER, owned and executable only by the
  sweeper login, returns ids only (tenant id, render id, the three versions) of renders still
  ``requested`` after ``min_age``. The routing check itself runs as the app role per tenant.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0028"
down_revision: str | None = "0027"
branch_labels = None
depends_on = None


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE render_episodes (
    id          uuid NOT NULL,
    tenant_id   uuid NOT NULL,
    render_id   uuid NOT NULL,
    kind        text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    ended_at    timestamptz,
    end_reason  text,
    detail      text,
    CONSTRAINT pk_render_episodes PRIMARY KEY (id),
    CONSTRAINT fk_render_episodes_tenant_id_render_id_renders FOREIGN KEY (tenant_id, render_id) REFERENCES renders (tenant_id, id),
    CONSTRAINT ck_render_episodes_kind CHECK (kind IN ('unroutable', 'sealing_stuck')),
    CONSTRAINT ck_render_episodes_end CHECK (
        (ended_at IS NULL) = (end_reason IS NULL)
        AND (end_reason IS NULL OR end_reason IN ('picked_up', 'worker_available', 'sealed', 'render_final')))
);
CREATE INDEX ix_render_episodes_render_id ON render_episodes (render_id);
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
ALTER TABLE render_episodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE render_episodes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON render_episodes USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_render_episodes_guard BEFORE UPDATE ON render_episodes FOR EACH ROW EXECUTE FUNCTION guard_render_episodes();
CREATE TRIGGER trg_render_episodes_no_delete BEFORE DELETE ON render_episodes FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON render_episodes TO "{app}";

-- a stuck seal that was flagged before this migration becomes an episode, then the column goes
INSERT INTO render_episodes (id, tenant_id, render_id, kind, started_at, ended_at, end_reason)
    SELECT gen_random_uuid(), tenant_id, id, 'sealing_stuck', sealing_stuck_at, sealed_at,
           CASE WHEN sealed_at IS NULL THEN NULL ELSE 'sealed' END
    FROM renders WHERE sealing_stuck_at IS NOT NULL;
ALTER TABLE renders DROP CONSTRAINT ck_renders_sealing, DROP COLUMN sealing_stuck_at,
    ADD CONSTRAINT ck_renders_seal_failures CHECK (seal_failures >= 0);

GRANT SELECT (id, tenant_id, status, created_at, renderer_version, unicode_version, tzdata_version)
    ON renders TO edisc_sweeper;
CREATE POLICY sweeper_requested ON renders FOR SELECT TO edisc_sweeper USING (status = 'requested');
CREATE FUNCTION stale_requested_renders(p_min_age interval, p_limit integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, render_id uuid, renderer_version text, unicode_version text, tzdata_version text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT r.tenant_id, r.id, r.renderer_version, r.unicode_version, r.tzdata_version FROM renders r
    WHERE r.status = 'requested' AND r.created_at <= now() - p_min_age
      AND (p_tenant IS NULL OR r.tenant_id = p_tenant)
    ORDER BY r.created_at LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION stale_requested_renders(interval, integer, uuid) FROM PUBLIC;
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION stale_requested_renders(interval, integer, uuid) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""


DOWNGRADE = """
DROP FUNCTION IF EXISTS stale_requested_renders(interval, integer, uuid);
DROP POLICY IF EXISTS sweeper_requested ON renders;
REVOKE SELECT (id, tenant_id, status, created_at, renderer_version, unicode_version, tzdata_version)
    ON renders FROM edisc_sweeper;
ALTER TABLE renders DROP CONSTRAINT IF EXISTS ck_renders_seal_failures, ADD COLUMN sealing_stuck_at timestamptz,
    ADD CONSTRAINT ck_renders_sealing CHECK (
        seal_failures >= 0 AND (sealing_stuck_at IS NULL OR status IN ('completed', 'refused', 'failed')));
DROP TABLE IF EXISTS render_episodes;
DROP FUNCTION IF EXISTS guard_render_episodes();
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
