"""Closing matters and clients (ADR 0002 amendment: who owns retention).

The retention-extension job keeps evidence locked while something still needs it: evidence of an
ACTIVE matter, and validated client-level exports while their client is ACTIVE. "Active" needs an end:
``closed_at`` / ``closed_by``, set once and never cleared. After closing, extension stops and objects
expire on schedule (the destruction workflow is separate, see docs/BACKLOG.md).

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0019"
down_revision: str | None = "0018"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE matters ADD COLUMN closed_at timestamptz, ADD COLUMN closed_by text,
    ADD CONSTRAINT ck_matters_closed CHECK ((closed_at IS NULL) = (closed_by IS NULL));
ALTER TABLE clients ADD COLUMN closed_at timestamptz, ADD COLUMN closed_by text,
    ADD CONSTRAINT ck_clients_closed CHECK ((closed_at IS NULL) = (closed_by IS NULL));

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

CREATE FUNCTION guard_clients() RETURNS trigger
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
CREATE TRIGGER trg_clients_guard BEFORE UPDATE ON clients FOR EACH ROW EXECUTE FUNCTION guard_clients();
"""

DOWNGRADE = """
DROP TRIGGER IF EXISTS trg_clients_guard ON clients;
DROP FUNCTION IF EXISTS guard_clients();
CREATE OR REPLACE FUNCTION guard_matters() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.retention_until < OLD.retention_until OR NEW.tenant_id <> OLD.tenant_id THEN
        RAISE EXCEPTION 'matters: retention_until may only be extended' USING ERRCODE = 'EA004';
    END IF;
    RETURN NEW;
END
$$;
ALTER TABLE clients DROP CONSTRAINT IF EXISTS ck_clients_closed, DROP COLUMN IF EXISTS closed_by, DROP COLUMN IF EXISTS closed_at;
ALTER TABLE matters DROP CONSTRAINT IF EXISTS ck_matters_closed, DROP COLUMN IF EXISTS closed_by, DROP COLUMN IF EXISTS closed_at;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
