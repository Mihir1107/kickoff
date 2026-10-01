"""Several scopes per job (ADR 0005 amendment, M13.5): which scopes cover each work unit.

Units are the union over a job's scopes; ``work_unit_scopes`` records the coverage. It decides the
unit's thread-parent policy and, through the scopes covering a conversation, which items are in scope.
Append-only in practice: rows are inserted at enumeration and never changed.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE work_unit_scopes (
    tenant_id  uuid NOT NULL,
    job_id     uuid NOT NULL,
    unit_key   text NOT NULL,
    scope_id   uuid NOT NULL,
    CONSTRAINT pk_work_unit_scopes PRIMARY KEY (job_id, unit_key, scope_id),
    CONSTRAINT fk_work_unit_scopes_job_id_unit_key_work_units FOREIGN KEY (job_id, unit_key) REFERENCES work_units (job_id, unit_key),
    CONSTRAINT fk_work_unit_scopes_scope_id_collection_scopes FOREIGN KEY (scope_id) REFERENCES collection_scopes (id)
);
ALTER TABLE work_unit_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_unit_scopes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON work_unit_scopes USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_work_unit_scopes_no_update BEFORE UPDATE ON work_unit_scopes FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_work_unit_scopes_no_delete BEFORE DELETE ON work_unit_scopes FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON work_unit_scopes TO "{app}";
"""


DOWNGRADE = "DROP TABLE IF EXISTS work_unit_scopes;"


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
