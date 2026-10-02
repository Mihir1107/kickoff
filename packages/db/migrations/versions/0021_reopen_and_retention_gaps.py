"""Reversible closing and recorded retention gaps (ADR 0002 amendment, 2026-10-02 review).

- A closed matter or client may be REOPENED (tenant admins, audited): ``closed_at``/``closed_by`` go
  back to NULL together. Changing who/when closed while closed is still refused.
- ``retention_gaps``: one row per object whose COMPLIANCE retention had lapsed when the
  retention-extension job next reached it (typically after a reopen): the unprotected window
  (``unprotected_from`` = the lapsed retain-until, ``unprotected_until`` = when it was re-locked or found
  gone), the outcome (``relocked`` or ``missing``) and the owner that needed it. Append-only: a gap is
  never hidden.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0021"
down_revision: str | None = "0020"
branch_labels = None
depends_on = None


def _upgrade(app: str) -> str:
    return f"""
CREATE OR REPLACE FUNCTION guard_matters() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.retention_until < OLD.retention_until OR NEW.tenant_id <> OLD.tenant_id THEN
        RAISE EXCEPTION 'matters: retention_until may only be extended' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NOT NULL AND NEW.closed_at IS NOT NULL
       AND (NEW.closed_at, NEW.closed_by) IS DISTINCT FROM (OLD.closed_at, OLD.closed_by) THEN
        RAISE EXCEPTION 'matters: reopen before closing again' USING ERRCODE = 'EA004';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION guard_clients() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.is_default <> OLD.is_default THEN
        RAISE EXCEPTION 'clients: tenant and default flag are immutable' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NOT NULL AND NEW.closed_at IS NOT NULL
       AND (NEW.closed_at, NEW.closed_by) IS DISTINCT FROM (OLD.closed_at, OLD.closed_by) THEN
        RAISE EXCEPTION 'clients: reopen before closing again' USING ERRCODE = 'EA004';
    END IF;
    RETURN NEW;
END
$$;

CREATE TABLE retention_gaps (
    id                  uuid NOT NULL,
    tenant_id           uuid NOT NULL,
    evidence_object_id  uuid NOT NULL,
    owner_type          text NOT NULL,
    owner_id            uuid NOT NULL,
    unprotected_from    timestamptz NOT NULL,
    unprotected_until   timestamptz NOT NULL,
    outcome             text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_retention_gaps PRIMARY KEY (id),
    CONSTRAINT fk_retention_gaps_tenant_id_evidence_object_id_evidence_objects FOREIGN KEY (tenant_id, evidence_object_id) REFERENCES evidence_objects (tenant_id, id),
    CONSTRAINT ck_retention_gaps_owner_type CHECK (owner_type IN ('matter', 'client')),
    CONSTRAINT ck_retention_gaps_outcome CHECK (outcome IN ('relocked', 'missing')),
    CONSTRAINT ck_retention_gaps_window CHECK (unprotected_until >= unprotected_from)
);
CREATE INDEX ix_retention_gaps_tenant_id_owner_id ON retention_gaps (tenant_id, owner_id);
ALTER TABLE retention_gaps ENABLE ROW LEVEL SECURITY;
ALTER TABLE retention_gaps FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON retention_gaps USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_retention_gaps_no_update BEFORE UPDATE ON retention_gaps FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_retention_gaps_no_delete BEFORE DELETE ON retention_gaps FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT ON retention_gaps TO "{app}";
"""


DOWNGRADE = """
DROP TABLE IF EXISTS retention_gaps;
CREATE OR REPLACE FUNCTION guard_matters() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.retention_until < OLD.retention_until OR NEW.tenant_id <> OLD.tenant_id THEN
        RAISE EXCEPTION 'matters: retention_until may only be extended' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NOT NULL
       AND (NEW.closed_at, NEW.closed_by) IS DISTINCT FROM (OLD.closed_at, OLD.closed_by) THEN
        RAISE EXCEPTION 'matters: a closed matter stays closed' USING ERRCODE = 'EA004';
    END IF;
    RETURN NEW;
END
$$;
CREATE OR REPLACE FUNCTION guard_clients() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.is_default <> OLD.is_default THEN
        RAISE EXCEPTION 'clients: tenant and default flag are immutable' USING ERRCODE = 'EA004';
    END IF;
    IF OLD.closed_at IS NOT NULL
       AND (NEW.closed_at, NEW.closed_by) IS DISTINCT FROM (OLD.closed_at, OLD.closed_by) THEN
        RAISE EXCEPTION 'clients: a closed client stays closed' USING ERRCODE = 'EA004';
    END IF;
    RETURN NEW;
END
$$;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
