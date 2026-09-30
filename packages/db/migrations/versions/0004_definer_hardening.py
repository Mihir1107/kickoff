"""Harden SECURITY DEFINER functions (review of M5.1).

- Fixed, safe search_path on every definer function: ``pg_catalog, edisc, pg_temp`` (pg_temp last,
  so temporary objects can never shadow catalog or schema objects).
- due_anchor_streams: EXECUTE only for its owner, the sweeper login. Revoked from edisc_app and PUBLIC.
- create_tenant: EXECUTE for edisc_app only, never PUBLIC.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None

SWEEP_FN = "due_anchor_streams(interval, integer, uuid)"
TENANT_FN = "create_tenant(uuid, text, text, text)"


def _upgrade(app_role: str) -> str:
    return f"""
ALTER FUNCTION {TENANT_FN} SET search_path = pg_catalog, edisc, pg_temp;
REVOKE ALL ON FUNCTION {TENANT_FN} FROM PUBLIC;
SET LOCAL ROLE edisc_sweeper;
ALTER FUNCTION {SWEEP_FN} SET search_path = pg_catalog, edisc, pg_temp;
REVOKE ALL ON FUNCTION {SWEEP_FN} FROM PUBLIC;
REVOKE ALL ON FUNCTION {SWEEP_FN} FROM "{app_role}";
RESET ROLE;
"""


def _downgrade(app_role: str) -> str:
    return f"""
SET LOCAL ROLE edisc_sweeper;
GRANT EXECUTE ON FUNCTION {SWEEP_FN} TO "{app_role}";
ALTER FUNCTION {SWEEP_FN} SET search_path = edisc, pg_temp;
RESET ROLE;
ALTER FUNCTION {TENANT_FN} SET search_path = edisc, pg_temp;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def _app_role() -> str:
    return str(op.get_context().config.attributes.get("app_role", "edisc_app"))  # type: ignore[union-attr]


def upgrade() -> None:
    _run(_upgrade(_app_role()))


def downgrade() -> None:
    _run(_downgrade(_app_role()))
