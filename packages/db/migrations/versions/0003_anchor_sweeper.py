"""Anchor sweeper support: list streams with overdue anchors across tenants, ids only.

Terminated or abandoned jobs have no next writer, so a periodic sweeper must seal their chain heads.
Tenant RLS hides other tenants' heads from the app role, and FORCE RLS applies to the owner too, so:

- ``due_anchor_streams(idle, limit)`` is SECURITY DEFINER, owned by the NOLOGIN ``edisc_sweeper`` role.
- ``edisc_sweeper`` has SELECT on a few columns of custody_chain_heads and a row policy that exposes
  only streams that are due (flag set) or idle with an unanchored tail.
- The function returns (tenant_id, stream_id) only; anchoring then runs through the normal tenant_tx.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def _upgrade_sql(app_role: str) -> str:
    return f"""
GRANT USAGE ON SCHEMA edisc TO edisc_sweeper;
-- the tenant_isolation policy (TO PUBLIC) is evaluated for this role too
GRANT EXECUTE ON FUNCTION current_tenant_id() TO edisc_sweeper;
GRANT SELECT (stream_id, tenant_id, last_seq, last_anchored_seq, anchor_due, updated_at)
    ON custody_chain_heads TO edisc_sweeper;
CREATE POLICY sweeper_overdue ON custody_chain_heads FOR SELECT TO edisc_sweeper
    USING (anchor_due OR last_seq > last_anchored_seq);

CREATE FUNCTION due_anchor_streams(p_idle interval, p_limit integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (tenant_id uuid, stream_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = edisc, pg_temp AS $$
    SELECT h.tenant_id, h.stream_id FROM custody_chain_heads h
    WHERE (p_tenant IS NULL OR h.tenant_id = p_tenant)
      AND (h.anchor_due OR (h.last_seq > h.last_anchored_seq AND h.updated_at <= now() - p_idle))
    ORDER BY h.updated_at
    LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION due_anchor_streams(interval, integer, uuid) FROM PUBLIC;
-- grant before handing ownership away: afterwards the migration role can no longer grant on it
GRANT EXECUTE ON FUNCTION due_anchor_streams(interval, integer, uuid) TO "{app_role}";
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION due_anchor_streams(interval, integer, uuid) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""


DOWNGRADE = """
DROP FUNCTION IF EXISTS due_anchor_streams(interval, integer, uuid);
DROP POLICY IF EXISTS sweeper_overdue ON custody_chain_heads;
REVOKE EXECUTE ON FUNCTION current_tenant_id() FROM edisc_sweeper;
REVOKE ALL ON custody_chain_heads FROM edisc_sweeper;
REVOKE USAGE ON SCHEMA edisc FROM edisc_sweeper;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade_sql(op.get_context().config.attributes.get("app_role", "edisc_app")))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
