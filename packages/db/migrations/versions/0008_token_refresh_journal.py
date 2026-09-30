"""Token refresh journal (ADR 0009): a provider's refresh response is persisted in its OWN transaction
immediately on receipt, before it is applied to the connection row. If the process dies between the
provider rotating the refresh token and our commit, the new pair is still durable and the startup
reconciler applies it (or marks it superseded).

- Blobs are sealed with the same (tenant, connection, purpose) context as the connection columns, so
  they are applied by copying ciphertext: no decryption during reconcile.
- Cross-tenant discovery for the reconciler: ``pending_token_refreshes()`` SECURITY DEFINER, owned and
  executable only by the sweeper login, returns ids only.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None


def _upgrade(app_role: str) -> str:
    return f"""
CREATE TABLE token_refresh_journal (
    id                       uuid        NOT NULL,
    tenant_id                uuid        NOT NULL,
    connection_id            uuid        NOT NULL,
    based_on_version         bigint      NOT NULL,
    encrypted_access_token   bytea       NOT NULL,
    encrypted_refresh_token  bytea,
    token_expires_at         timestamptz,
    token_key_id             text        NOT NULL,
    token_key_version        text        NOT NULL,
    state                    text        NOT NULL DEFAULT 'received',
    created_at               timestamptz NOT NULL DEFAULT now(),
    resolved_at              timestamptz,
    CONSTRAINT pk_token_refresh_journal PRIMARY KEY (id),
    CONSTRAINT fk_token_refresh_journal_tenant_id_connection_id_connections
        FOREIGN KEY (tenant_id, connection_id) REFERENCES connections (tenant_id, id),
    CONSTRAINT ck_token_refresh_journal_state CHECK (state IN ('received', 'applied', 'superseded'))
);
CREATE INDEX ix_token_refresh_journal_state ON token_refresh_journal (state);
ALTER TABLE token_refresh_journal ENABLE ROW LEVEL SECURITY;
ALTER TABLE token_refresh_journal FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON token_refresh_journal
    USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_token_refresh_journal_no_delete BEFORE DELETE ON token_refresh_journal
    FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_token_refresh_journal_no_truncate BEFORE TRUNCATE ON token_refresh_journal
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON token_refresh_journal TO "{app_role}";

GRANT SELECT (tenant_id, connection_id, state, created_at) ON token_refresh_journal TO edisc_sweeper;
CREATE POLICY sweeper_received ON token_refresh_journal FOR SELECT TO edisc_sweeper USING (state = 'received');
CREATE FUNCTION pending_token_refreshes(p_limit integer)
RETURNS TABLE (tenant_id uuid, connection_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT DISTINCT j.tenant_id, j.connection_id FROM token_refresh_journal j
    WHERE j.state = 'received' LIMIT p_limit
$$;
REVOKE ALL ON FUNCTION pending_token_refreshes(integer) FROM PUBLIC;
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION pending_token_refreshes(integer) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""


DOWNGRADE = """
DROP FUNCTION IF EXISTS pending_token_refreshes(integer);
DROP TABLE IF EXISTS token_refresh_journal;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
