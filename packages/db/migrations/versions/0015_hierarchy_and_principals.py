"""Hierarchy and principals for the API (ADR 0013, M13.1).

- ``clients`` (tenant > client > matter > workspace). Matters and connections belong to a client
  (decision a: connections are client-owned and shared by the client's matters). Existing rows, and
  inserts that name no client (internal tools, tests), go to the tenant's DEFAULT client, created on
  first use by a trigger. The API always names the client explicitly.
- ``workspaces`` under matters (decision c: modelled now, no features yet); a job may name one.
- ``tenant_idps``: the tenant's OIDC issuers (issuer, audience, JWKS URL, groups claim).
- ``principals``: users and service accounts, identified by (issuer, subject) within the tenant.
- ``groups`` (an IdP group claim value and/or local membership) and ``group_members``.
- ``role_assignments``: (principal | group) x fixed role x scope (tenant | client | matter | workspace).
  Memberships and assignments are never deleted: they are ended (``removed_at`` / ``revoked_at``), so
  who could do what, when, stays reconstructible.
- ``api_idempotency``: Idempotency-Key records for job creation.
- ``tenant_id_for_subdomain(text)``: SECURITY DEFINER, ids only, so the API can resolve the Host
  subdomain to a tenant before any tenant context exists. Owned by the sweeper login (which may see
  only tenant ids and subdomains); executable by the app role.

Every new table: FORCE RLS on tenant_id, app grants only as needed. Audit events reuse the custody
chain with stream id = tenant id (ADR 0013 section 4), so no table is added for them.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None

ROLES = "'tenant_admin', 'client_admin', 'matter_manager', 'collector', 'reviewer', 'auditor'"
SCOPES = "'tenant', 'client', 'matter', 'workspace'"
NEW_TABLES = [
    "clients",
    "workspaces",
    "tenant_idps",
    "principals",
    "groups",
    "group_members",
    "role_assignments",
    "api_idempotency",
]


def _rls(table: str) -> str:
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;\n"
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;\n"
        f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = current_tenant_id())"
        " WITH CHECK (tenant_id = current_tenant_id());\n"
    )


def _upgrade(app: str) -> str:
    return f"""
CREATE TABLE clients (
    id          uuid        NOT NULL,
    tenant_id   uuid        NOT NULL,
    name        text        NOT NULL,
    is_default  boolean     NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_clients PRIMARY KEY (id),
    CONSTRAINT uq_clients_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_clients_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id)
);
CREATE UNIQUE INDEX uq_clients_one_default ON clients (tenant_id) WHERE is_default;

CREATE TABLE workspaces (
    id          uuid        NOT NULL,
    tenant_id   uuid        NOT NULL,
    matter_id   uuid        NOT NULL,
    name        text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_workspaces PRIMARY KEY (id),
    CONSTRAINT uq_workspaces_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_workspaces_tenant_id_matter_id_matters FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id)
);

CREATE TABLE tenant_idps (
    id            uuid        NOT NULL,
    tenant_id     uuid        NOT NULL,
    issuer        text        NOT NULL,
    audience      text        NOT NULL,
    jwks_url      text        NOT NULL,
    groups_claim  text        NOT NULL DEFAULT 'groups',
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_tenant_idps PRIMARY KEY (id),
    CONSTRAINT uq_tenant_idps_tenant_id_issuer UNIQUE (tenant_id, issuer),
    CONSTRAINT fk_tenant_idps_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id)
);

CREATE TABLE principals (
    id            uuid        NOT NULL,
    tenant_id     uuid        NOT NULL,
    kind          text        NOT NULL,
    issuer        text        NOT NULL,
    subject       text        NOT NULL,
    display_name  text        NOT NULL,
    email         text,
    active        boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_principals PRIMARY KEY (id),
    CONSTRAINT uq_principals_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_principals_tenant_id_issuer_subject UNIQUE (tenant_id, issuer, subject),
    CONSTRAINT ck_principals_kind CHECK (kind IN ('user', 'service')),
    CONSTRAINT fk_principals_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id)
);

CREATE TABLE groups (
    id           uuid        NOT NULL,
    tenant_id    uuid        NOT NULL,
    name         text        NOT NULL,
    external_id  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_groups PRIMARY KEY (id),
    CONSTRAINT uq_groups_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_groups_tenant_id_name UNIQUE (tenant_id, name),
    CONSTRAINT uq_groups_tenant_id_external_id UNIQUE (tenant_id, external_id),
    CONSTRAINT fk_groups_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id)
);

CREATE TABLE group_members (
    id            uuid        NOT NULL,
    tenant_id     uuid        NOT NULL,
    group_id      uuid        NOT NULL,
    principal_id  uuid        NOT NULL,
    added_at      timestamptz NOT NULL DEFAULT now(),
    removed_at    timestamptz,
    CONSTRAINT pk_group_members PRIMARY KEY (id),
    CONSTRAINT fk_group_members_tenant_id_group_id_groups FOREIGN KEY (tenant_id, group_id) REFERENCES groups (tenant_id, id),
    CONSTRAINT fk_group_members_tenant_id_principal_id_principals FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, id)
);

CREATE TABLE role_assignments (
    id            uuid        NOT NULL,
    tenant_id     uuid        NOT NULL,
    principal_id  uuid,
    group_id      uuid,
    role          text        NOT NULL,
    scope_type    text        NOT NULL,
    scope_id      uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text        NOT NULL,
    revoked_at    timestamptz,
    revoked_by    text,
    CONSTRAINT pk_role_assignments PRIMARY KEY (id),
    CONSTRAINT ck_role_assignments_revocation CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
    CONSTRAINT ck_role_assignments_one_holder CHECK ((principal_id IS NULL) <> (group_id IS NULL)),
    CONSTRAINT ck_role_assignments_role CHECK (role IN ({ROLES})),
    CONSTRAINT ck_role_assignments_scope_type CHECK (scope_type IN ({SCOPES})),
    CONSTRAINT ck_role_assignments_scope_id CHECK ((scope_type = 'tenant') = (scope_id IS NULL)),
    CONSTRAINT fk_role_assignments_tenant_id_principal_id_principals FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, id),
    CONSTRAINT fk_role_assignments_tenant_id_group_id_groups FOREIGN KEY (tenant_id, group_id) REFERENCES groups (tenant_id, id)
);
CREATE UNIQUE INDEX uq_group_members_active ON group_members (group_id, principal_id) WHERE removed_at IS NULL;
CREATE INDEX ix_role_assignments_tenant_id_principal_id ON role_assignments (tenant_id, principal_id);
CREATE INDEX ix_role_assignments_tenant_id_group_id ON role_assignments (tenant_id, group_id);

CREATE TABLE api_idempotency (
    tenant_id     uuid        NOT NULL,
    key           text        NOT NULL,
    principal_id  uuid        NOT NULL,
    request_hash  text        NOT NULL,
    job_id        uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_api_idempotency PRIMARY KEY (tenant_id, key),
    CONSTRAINT ck_api_idempotency_key CHECK (length(key) BETWEEN 1 AND 255),
    CONSTRAINT fk_api_idempotency_tenant_id_job_id_collection_jobs FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id)
);

ALTER TABLE matters ADD COLUMN client_id uuid;
ALTER TABLE connections ADD COLUMN client_id uuid;
ALTER TABLE collection_jobs ADD COLUMN workspace_id uuid,
    ADD CONSTRAINT fk_collection_jobs_tenant_id_workspace_id_workspaces
        FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id);

-- backfill: one default client per existing tenant (RLS is relaxed for the owner inside this transaction only)
ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE matters NO FORCE ROW LEVEL SECURITY;
ALTER TABLE connections NO FORCE ROW LEVEL SECURITY;
INSERT INTO clients (id, tenant_id, name, is_default) SELECT gen_random_uuid(), id, 'Default client', true FROM tenants;
UPDATE matters m SET client_id = c.id FROM clients c WHERE c.tenant_id = m.tenant_id AND c.is_default;
UPDATE connections x SET client_id = c.id FROM clients c WHERE c.tenant_id = x.tenant_id AND c.is_default;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE matters FORCE ROW LEVEL SECURITY;
ALTER TABLE connections FORCE ROW LEVEL SECURITY;

ALTER TABLE matters ALTER COLUMN client_id SET NOT NULL,
    ADD CONSTRAINT fk_matters_tenant_id_client_id_clients FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id);
ALTER TABLE connections ALTER COLUMN client_id SET NOT NULL,
    ADD CONSTRAINT fk_connections_tenant_id_client_id_clients FOREIGN KEY (tenant_id, client_id) REFERENCES clients (tenant_id, id);

CREATE FUNCTION assign_default_client() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.client_id IS NULL THEN
        INSERT INTO clients (id, tenant_id, name, is_default)
            VALUES (gen_random_uuid(), NEW.tenant_id, 'Default client', true)
            ON CONFLICT (tenant_id) WHERE is_default DO NOTHING;
        SELECT id INTO NEW.client_id FROM clients WHERE tenant_id = NEW.tenant_id AND is_default;
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER trg_matters_default_client BEFORE INSERT ON matters FOR EACH ROW EXECUTE FUNCTION assign_default_client();
CREATE TRIGGER trg_connections_default_client BEFORE INSERT ON connections FOR EACH ROW EXECUTE FUNCTION assign_default_client();

{"".join(_rls(t) for t in NEW_TABLES)}
CREATE TRIGGER trg_clients_no_delete BEFORE DELETE ON clients FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_workspaces_no_delete BEFORE DELETE ON workspaces FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER trg_principals_no_delete BEFORE DELETE ON principals FOR EACH ROW EXECUTE FUNCTION reject_mutation();
-- nothing is deleted: memberships and role assignments are ended (removed_at / revoked_at), keeping history
GRANT SELECT, INSERT, UPDATE ON clients, workspaces, tenant_idps, principals, groups, group_members, role_assignments TO "{app}";
GRANT SELECT, INSERT, UPDATE ON api_idempotency TO "{app}";

CREATE FUNCTION tenant_id_for_subdomain(p_subdomain text) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, edisc, pg_temp AS $$
    SELECT id FROM tenants WHERE subdomain = p_subdomain
$$;
REVOKE ALL ON FUNCTION tenant_id_for_subdomain(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tenant_id_for_subdomain(text) TO "{app}";  -- before the owner change
-- owned by the sweeper login (like the other cross-tenant id lookups): it sees only (id, subdomain)
GRANT SELECT (id, subdomain) ON tenants TO edisc_sweeper;
CREATE POLICY sweeper_resolve ON tenants FOR SELECT TO edisc_sweeper USING (true);
GRANT CREATE ON SCHEMA edisc TO edisc_sweeper;
ALTER FUNCTION tenant_id_for_subdomain(text) OWNER TO edisc_sweeper;
REVOKE CREATE ON SCHEMA edisc FROM edisc_sweeper;
"""


DOWNGRADE = f"""
DROP FUNCTION IF EXISTS tenant_id_for_subdomain(text);
DROP POLICY IF EXISTS sweeper_resolve ON tenants;
REVOKE SELECT (id, subdomain) ON tenants FROM edisc_sweeper;
DROP TRIGGER IF EXISTS trg_connections_default_client ON connections;
DROP TRIGGER IF EXISTS trg_matters_default_client ON matters;
DROP FUNCTION IF EXISTS assign_default_client();
ALTER TABLE collection_jobs DROP CONSTRAINT IF EXISTS fk_collection_jobs_tenant_id_workspace_id_workspaces,
    DROP COLUMN IF EXISTS workspace_id;
ALTER TABLE connections DROP CONSTRAINT IF EXISTS fk_connections_tenant_id_client_id_clients, DROP COLUMN IF EXISTS client_id;
ALTER TABLE matters DROP CONSTRAINT IF EXISTS fk_matters_tenant_id_client_id_clients, DROP COLUMN IF EXISTS client_id;
DROP TABLE IF EXISTS {", ".join(reversed(NEW_TABLES))};
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
