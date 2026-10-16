"""Source install flows (M17 step 6; ADR 0016 §5, A2, A3, A4, A2.3).

- ``oauth_installs``: one row per started install (Slack OAuth v2, Microsoft admin consent through
  OIDC). Holds the SHA-256 of the install cookie's handle, the nonce, the key id the state's MAC and
  the PKCE verifier are derived under (no secret is stored), the acting session and principal, the
  client and the connection (a ``pending`` one for a new install, the existing one for a
  re-authorization). Insert-only, plus one consume and one outcome (guard trigger), like
  ``login_attempts``.
- ``external_bindings``: the CROSS-TENANT registry of external ids (Entra ``tid``, Slack team id,
  Slack enterprise id): one of our tenants per external id (A4). Not a tenant table: the app role
  has NO privilege on it; it writes only through ``bind_external`` (SECURITY DEFINER, binds to the
  CALLER's tenant from ``app.tenant_id``, never a parameter) and moves happen only through the
  operator CLI (``edisc_api.admin move-binding``, A2.3) as the owner.
- ``oauth_install_route(uuid)``: the relay host's lookup (A3): for an UNCONSUMED, UNEXPIRED install,
  its tenant, principal, nonce, key id, expiry, provider and the tenant's subdomain; nothing else.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0040"
down_revision: str | None = "0039"
branch_labels = None
depends_on = None

OUTCOMES = (
    "('completed', 'denied', 'expired', 'replayed', 'mismatch', 'failed', 'refused',"
    " 'already_bound')"
)


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE oauth_installs (
    id                uuid NOT NULL,
    tenant_id         uuid NOT NULL,
    provider          text NOT NULL,
    client_id         uuid NOT NULL,
    connection_id     uuid NOT NULL,
    reauth            boolean NOT NULL,
    session_id        uuid NOT NULL,
    principal_id      uuid NOT NULL,
    handle_hash       bytea NOT NULL,
    key_id            text NOT NULL,
    nonce             text NOT NULL,
    requested_scopes  text[] NOT NULL,
    request_id        text,
    started_at        timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz NOT NULL,
    consumed_at       timestamptz,
    outcome           text,
    outcome_detail    text,
    external_id       text COLLATE "C",
    CONSTRAINT pk_oauth_installs PRIMARY KEY (id),
    CONSTRAINT uq_oauth_installs_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_oauth_installs_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_oauth_installs_tenant_id_client_id_clients
        FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id),
    CONSTRAINT fk_oauth_installs_tenant_id_connection_id_connections
        FOREIGN KEY (tenant_id, connection_id) REFERENCES connections (tenant_id, id),
    CONSTRAINT fk_oauth_installs_tenant_id_session_id_sessions
        FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id),
    CONSTRAINT fk_oauth_installs_tenant_id_principal_id_principals
        FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, id),
    CONSTRAINT ck_oauth_installs_provider CHECK (provider IN ('slack', 'microsoft')),
    CONSTRAINT ck_oauth_installs_handle CHECK (octet_length(handle_hash) = 32),
    CONSTRAINT ck_oauth_installs_expiry CHECK (expires_at > started_at),
    CONSTRAINT ck_oauth_installs_outcome CHECK (outcome IS NULL OR (consumed_at IS NOT NULL
        AND outcome IN {OUTCOMES})),
    CONSTRAINT ck_oauth_installs_detail CHECK (outcome_detail IS NULL
        OR outcome_detail ~ '^[a-z_]{{1,64}}$')
);
CREATE INDEX ix_oauth_installs_connection ON oauth_installs (tenant_id, connection_id);

CREATE OR REPLACE FUNCTION guard_oauth_installs() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.provider, NEW.client_id, NEW.connection_id, NEW.reauth,
        NEW.session_id, NEW.principal_id, NEW.handle_hash, NEW.key_id, NEW.nonce,
        NEW.requested_scopes, NEW.request_id, NEW.started_at, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.provider, OLD.client_id, OLD.connection_id, OLD.reauth,
        OLD.session_id, OLD.principal_id, OLD.handle_hash, OLD.key_id, OLD.nonce,
        OLD.requested_scopes, OLD.request_id, OLD.started_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'oauth_installs: identity columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
        RAISE EXCEPTION 'oauth_installs: consumed once' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.outcome IS NOT NULL AND (NEW.outcome, NEW.outcome_detail, NEW.external_id)
       IS DISTINCT FROM (OLD.outcome, OLD.outcome_detail, OLD.external_id) THEN
        RAISE EXCEPTION 'oauth_installs: the outcome is final' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE oauth_installs ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_installs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON oauth_installs USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_oauth_installs_guard BEFORE UPDATE ON oauth_installs FOR EACH ROW EXECUTE FUNCTION guard_oauth_installs();
CREATE TRIGGER trg_oauth_installs_no_delete BEFORE DELETE ON oauth_installs FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON oauth_installs TO "{app}";

CREATE TABLE external_bindings (
    provider          text NOT NULL,
    external_id       text COLLATE "C" NOT NULL,
    bound_tenant_id   uuid NOT NULL,
    connection_id     uuid,  -- NULL after an operator move, until the destination installs
    bound_at          timestamptz NOT NULL DEFAULT now(),
    bound_by          text NOT NULL,
    CONSTRAINT pk_external_bindings PRIMARY KEY (provider, external_id),
    CONSTRAINT fk_external_bindings_bound_tenant_id_tenants
        FOREIGN KEY (bound_tenant_id) REFERENCES tenants (id),
    CONSTRAINT ck_external_bindings_provider CHECK (provider IN ('slack_team', 'slack_enterprise',
        'microsoft_tenant')),
    CONSTRAINT ck_external_bindings_external_id CHECK (external_id ~ '^[A-Za-z0-9-]{{1,64}}$')
);
REVOKE ALL ON external_bindings FROM PUBLIC;
GRANT SELECT, INSERT ON external_bindings TO edisc_sweeper;
GRANT UPDATE (connection_id) ON external_bindings TO edisc_sweeper;  -- the bind after a move

-- binds an external id to the CALLER's tenant (app.tenant_id, set by tenant_tx), unless another
-- tenant holds it: returns the tenant that holds it after the call (the caller compares)
CREATE FUNCTION bind_external(p_provider text, p_external_id text, p_connection uuid, p_actor text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
DECLARE
    v_tenant uuid := nullif(current_setting('app.tenant_id', true), '')::uuid;
    v_holder uuid;
BEGIN
    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'bind_external: no tenant context' USING ERRCODE = 'EA002';
    END IF;
    INSERT INTO external_bindings (provider, external_id, bound_tenant_id, connection_id, bound_by)
    VALUES (p_provider, p_external_id, v_tenant, p_connection, p_actor)
    ON CONFLICT (provider, external_id) DO UPDATE SET connection_id = p_connection
        WHERE external_bindings.bound_tenant_id = v_tenant AND external_bindings.connection_id IS NULL;
    SELECT bound_tenant_id INTO v_holder FROM external_bindings
    WHERE provider = p_provider AND external_id = p_external_id;
    RETURN v_holder;
END $$;
REVOKE ALL ON FUNCTION bind_external(text, text, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bind_external(text, text, uuid, text) TO "{app}";

CREATE FUNCTION oauth_install_route(p_install uuid)
RETURNS TABLE (tenant_id uuid, principal_id uuid, nonce text, key_id text,
               expires_at timestamptz, provider text, subdomain text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT i.tenant_id, i.principal_id, i.nonce, i.key_id, i.expires_at, i.provider, t.subdomain
    FROM oauth_installs i JOIN tenants t ON t.id = i.tenant_id
    WHERE i.id = p_install AND i.consumed_at IS NULL AND i.expires_at > now()
$$;
REVOKE ALL ON FUNCTION oauth_install_route(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oauth_install_route(uuid) TO "{app}";
GRANT SELECT (id, tenant_id, principal_id, nonce, key_id, expires_at, provider, consumed_at)
    ON oauth_installs TO edisc_sweeper;
CREATE POLICY sweeper_route ON oauth_installs FOR SELECT TO edisc_sweeper
    USING (consumed_at IS NULL);

-- owned by the sweeper login (it may insert bindings and read unconsumed installs, nothing else)
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION bind_external(text, text, uuid, text) OWNER TO edisc_sweeper;
ALTER FUNCTION oauth_install_route(uuid) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""


DOWNGRADE = """
DROP FUNCTION IF EXISTS oauth_install_route(uuid);
DROP FUNCTION IF EXISTS bind_external(text, text, uuid, text);
DROP TABLE IF EXISTS external_bindings;
DROP TABLE IF EXISTS oauth_installs;
DROP FUNCTION IF EXISTS guard_oauth_installs();
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
