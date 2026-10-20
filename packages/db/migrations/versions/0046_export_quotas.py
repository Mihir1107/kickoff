"""Per-tenant upload quotas for Slack exports, and upload expiry (M18 §4, D10).

- ``tenant_quotas``: a tenant's limits, set ONLY by the operator CLI (``python -m edisc_api.admin
  quota``, as the migration owner); the app role may read them. No row = the defaults
  (``EDISC_EXPORT_QUOTA_*``; D10: 200 GiB in flight, 2 TiB stored, 3 concurrent uploads).
  ``tenant_quota_changes`` records every change (who, why, before, after); append-only.
- Usage is never a stored counter: it is computed from ``slack_exports`` under a per-tenant
  transaction advisory lock in the create transaction (``edisc_api.quotas``), so it cannot drift.
  In flight = uploading/locking/validating (declared sizes); stored = ready exports of open
  clients, plus in flight.
- An upload not completed within the upload TTL is EXPIRED by the ``expire-export-uploads`` sweep:
  ``uploading -> rejected`` with reason ``upload_expired`` (a new transition and reason), which
  releases its quota. Its staged parts expire with the staging lifecycle.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0046"
down_revision: str | None = "0045"
branch_labels = None
depends_on = None

GUARD = """
CREATE OR REPLACE FUNCTION guard_slack_exports() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.client_id, NEW.declared_size, NEW.declared_sha256, NEW.declared_plan,
        NEW.limits, NEW.staging_key, NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.client_id, OLD.declared_size, OLD.declared_sha256, OLD.declared_plan,
        OLD.limits, OLD.staging_key, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'slack_exports: declared columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.sha256 IS NOT NULL
       AND (NEW.sha256, NEW.size_bytes, NEW.evidence_object_id, NEW.version_id, NEW.locked_at)
           IS DISTINCT FROM (OLD.sha256, OLD.size_bytes, OLD.evidence_object_id, OLD.version_id, OLD.locked_at) THEN
        RAISE EXCEPTION 'slack_exports: the locked archive is immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.status IN ('ready', 'rejected') THEN
        RAISE EXCEPTION 'slack_exports: % export % is final', OLD.status, OLD.id USING ERRCODE = 'EA002';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN (
        ('uploading', 'locking'), ('locking', 'uploading'), ('locking', 'validating'), ('locking', 'rejected'),
        ('validating', 'ready'), ('validating', 'rejected'), ('uploading', 'rejected')) THEN
        RAISE EXCEPTION 'slack_exports: % -> % is not allowed', OLD.status, NEW.status USING ERRCODE = 'EA002';
    END IF;
    IF OLD.status = 'uploading' AND NEW.status = 'rejected' AND NEW.reject_reason <> 'upload_expired' THEN
        RAISE EXCEPTION 'slack_exports: an upload is only rejected by expiry' USING ERRCODE = 'EA002';
    END IF;
    IF NEW.status = 'uploading' AND NEW.sha256 IS NOT NULL THEN
        RAISE EXCEPTION 'slack_exports: a locked export cannot be reopened' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
"""


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE tenant_quotas (
    tenant_id                  uuid NOT NULL,
    export_bytes_in_flight     bigint NOT NULL,
    export_bytes_stored        bigint NOT NULL,
    export_uploads_concurrent  integer NOT NULL,
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_tenant_quotas PRIMARY KEY (tenant_id),
    CONSTRAINT fk_tenant_quotas_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT ck_tenant_quotas_positive CHECK (
        export_bytes_in_flight > 0 AND export_bytes_stored > 0 AND export_uploads_concurrent > 0)
);
ALTER TABLE tenant_quotas ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_quotas FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_quotas USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_tenant_quotas_no_delete BEFORE DELETE ON tenant_quotas FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT ON tenant_quotas TO "{app}";

CREATE TABLE tenant_quota_changes (
    id          uuid NOT NULL,
    tenant_id   uuid NOT NULL,
    operator    text NOT NULL,
    reason      text NOT NULL,
    before      jsonb,
    after       jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_tenant_quota_changes PRIMARY KEY (id),
    CONSTRAINT fk_tenant_quota_changes_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT ck_tenant_quota_changes_reason CHECK (length(reason) BETWEEN 1 AND 2000)
);
CREATE INDEX ix_tenant_quota_changes_tenant_id ON tenant_quota_changes (tenant_id);
ALTER TABLE tenant_quota_changes ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_quota_changes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_quota_changes USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_tenant_quota_changes_no_update BEFORE UPDATE ON tenant_quota_changes FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_tenant_quota_changes_no_delete BEFORE DELETE ON tenant_quota_changes FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT ON tenant_quota_changes TO "{app}";

ALTER TABLE slack_exports DROP CONSTRAINT ck_slack_exports_reject_reason;
ALTER TABLE slack_exports ADD CONSTRAINT ck_slack_exports_reject_reason CHECK (
    (status = 'rejected') = (reject_reason IS NOT NULL)
    AND (reject_reason IS NULL OR reject_reason IN ('declared_hash_mismatch', 'archive_invalid', 'upload_expired')));
-- an upload rejected by expiry was never locked
ALTER TABLE slack_exports DROP CONSTRAINT ck_slack_exports_locked;
ALTER TABLE slack_exports ADD CONSTRAINT ck_slack_exports_locked CHECK (
    status IN ('uploading', 'locking')
    OR (status = 'rejected' AND reject_reason = 'upload_expired' AND sha256 IS NULL)
    OR (sha256 ~ '^[0-9a-f]{{64}}$' AND size_bytes >= 0 AND evidence_object_id IS NOT NULL
        AND version_id IS NOT NULL AND locked_at IS NOT NULL));
-- the quota's usage query and the expiry sweep: a tenant's exports by status
CREATE INDEX ix_slack_exports_tenant_id_status ON slack_exports (tenant_id, status);
{GUARD}
"""


DOWNGRADE = """
DROP TABLE IF EXISTS tenant_quota_changes;
DROP TABLE IF EXISTS tenant_quotas;
DROP INDEX IF EXISTS ix_slack_exports_tenant_id_status;
ALTER TABLE slack_exports DROP CONSTRAINT ck_slack_exports_locked;
ALTER TABLE slack_exports ADD CONSTRAINT ck_slack_exports_locked CHECK (
    status IN ('uploading', 'locking')
    OR (sha256 ~ '^[0-9a-f]{64}$' AND size_bytes >= 0 AND evidence_object_id IS NOT NULL
        AND version_id IS NOT NULL AND locked_at IS NOT NULL));
ALTER TABLE slack_exports DROP CONSTRAINT ck_slack_exports_reject_reason;
ALTER TABLE slack_exports ADD CONSTRAINT ck_slack_exports_reject_reason CHECK (
    (status = 'rejected') = (reject_reason IS NOT NULL)
    AND (reject_reason IS NULL OR reject_reason IN ('declared_hash_mismatch', 'archive_invalid')));
""" + GUARD.replace(", ('uploading', 'rejected')", "").replace(
    """    IF OLD.status = 'uploading' AND NEW.status = 'rejected' AND NEW.reject_reason <> 'upload_expired' THEN
        RAISE EXCEPTION 'slack_exports: an upload is only rejected by expiry' USING ERRCODE = 'EA002';
    END IF;
""",
    "",
)


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
