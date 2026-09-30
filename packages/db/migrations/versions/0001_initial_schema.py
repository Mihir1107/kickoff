"""Initial schema: tenancy, jobs, work units, evidence registry, items, custody log.

Security model (ADR 0003, 0006, 0007):
- Runs as the owner role. The app role gets only the grants listed at the bottom: no DELETE, no
  TRUNCATE, no DDL anywhere.
- Every tenant-scoped table: ENABLE + FORCE ROW LEVEL SECURITY with policy
  ``tenant_id = edisc.current_tenant_id()`` for USING and WITH CHECK.
- Append-only tables (custody_events, items, job_items) reject UPDATE, DELETE (row triggers) and
  TRUNCATE (statement trigger). Triggers fire for the owner too.
- Guarded-mutable tables: evidence_objects (pending -> complete|missing only), custody_chain_heads
  (seq advances by exactly one), matters (retention may only be extended).
- Freely mutable: collection_jobs, work_units (checkpoints/counters), connections, custodians.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None

HEX64 = "~ '^[0-9a-f]{64}$'"

TENANT_TABLES = [
    "matters",
    "connections",
    "custodians",
    "custodian_identities",
    "collection_jobs",
    "collection_scopes",
    "work_units",
    "evidence_objects",
    "items",
    "job_items",
    "custody_events",
    "custody_chain_heads",
]
APPEND_ONLY_TABLES = ["custody_events", "items", "job_items"]
NO_DELETE_TABLES = ["evidence_objects", "custody_chain_heads", "matters", "tenants"]

UPGRADE_SQL = f"""
-- ------------------------------------------------------------------ helper functions
CREATE FUNCTION current_tenant_id() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE FUNCTION reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only: % on %.% is not allowed', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'EA001';
END
$$;

-- ------------------------------------------------------------------ tenancy
CREATE TABLE tenants (
    id           uuid        NOT NULL,
    name         text        NOT NULL,
    subdomain    text        NOT NULL,
    kms_key_ref  text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_tenants PRIMARY KEY (id),
    CONSTRAINT uq_tenants_subdomain UNIQUE (subdomain),
    CONSTRAINT ck_tenants_subdomain_format CHECK (subdomain ~ '^[a-z0-9][a-z0-9-]{{1,62}}$')
);

CREATE TABLE matters (
    id               uuid        NOT NULL,
    tenant_id        uuid        NOT NULL,
    name             text        NOT NULL,
    retention_until  timestamptz NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_matters PRIMARY KEY (id),
    CONSTRAINT uq_matters_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_matters_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT ck_matters_retention_after_creation CHECK (retention_until > created_at)
);

CREATE TABLE connections (
    id                    uuid        NOT NULL,
    tenant_id             uuid        NOT NULL,
    source                text        NOT NULL,
    external_org_id       text        NOT NULL,
    plan_tier             text,
    granted_scopes        text[]      NOT NULL DEFAULT '{{}}',
    encrypted_token_blob  bytea,
    status                text        NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_connections PRIMARY KEY (id),
    CONSTRAINT uq_connections_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_connections_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT ck_connections_status CHECK (status IN ('pending', 'active', 'revoked', 'error'))
);

CREATE TABLE custodians (
    id             uuid        NOT NULL,
    tenant_id      uuid        NOT NULL,
    display_name   text        NOT NULL,
    primary_email  text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_custodians PRIMARY KEY (id),
    CONSTRAINT uq_custodians_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_custodians_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id)
);

CREATE TABLE custodian_identities (
    id                uuid        NOT NULL,
    tenant_id         uuid        NOT NULL,
    custodian_id      uuid        NOT NULL,
    source            text        NOT NULL,
    external_user_id  text        NOT NULL,
    email             text,
    merged_by         text,
    merged_at         timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_custodian_identities PRIMARY KEY (id),
    CONSTRAINT uq_custodian_identities_tenant_id_source_external_user_id
        UNIQUE (tenant_id, source, external_user_id),
    CONSTRAINT fk_custodian_identities_tenant_id_custodian_id_custodians
        FOREIGN KEY (tenant_id, custodian_id) REFERENCES custodians (tenant_id, id)
);

-- ------------------------------------------------------------------ jobs
CREATE TABLE collection_jobs (
    id                 uuid        NOT NULL,
    tenant_id          uuid        NOT NULL,
    matter_id          uuid        NOT NULL,
    connection_id      uuid        NOT NULL,
    status             text        NOT NULL,
    access_tier        text,
    connector_version  text        NOT NULL,
    requested_by       text        NOT NULL,
    status_detail      jsonb       NOT NULL DEFAULT '{{}}',
    seal_storage_key   text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    started_at         timestamptz,
    finished_at        timestamptz,
    CONSTRAINT pk_collection_jobs PRIMARY KEY (id),
    CONSTRAINT uq_collection_jobs_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_collection_jobs_tenant_id_matter_id_matters
        FOREIGN KEY (tenant_id, matter_id) REFERENCES matters (tenant_id, id),
    CONSTRAINT fk_collection_jobs_tenant_id_connection_id_connections
        FOREIGN KEY (tenant_id, connection_id) REFERENCES connections (tenant_id, id),
    CONSTRAINT ck_collection_jobs_status CHECK (status IN (
        'pending', 'running', 'completed', 'completed_with_gaps', 'completed_unverified',
        'failed', 'cancelled'))
);

CREATE TABLE collection_scopes (
    id           uuid        NOT NULL,
    tenant_id    uuid        NOT NULL,
    job_id       uuid        NOT NULL,
    scope_type   text        NOT NULL,
    external_id  text        NOT NULL,
    date_from    timestamptz NOT NULL,
    date_to      timestamptz NOT NULL,
    CONSTRAINT pk_collection_scopes PRIMARY KEY (id),
    CONSTRAINT fk_collection_scopes_tenant_id_job_id_collection_jobs
        FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    CONSTRAINT ck_collection_scopes_scope_type CHECK (scope_type IN ('custodian', 'channel', 'chat')),
    CONSTRAINT ck_collection_scopes_date_range CHECK (date_from < date_to)
);

-- work_units = checkpoint + reconciliation per (job, conversation x UTC day) (ADR 0005)
CREATE TABLE work_units (
    tenant_id        uuid        NOT NULL,
    job_id           uuid        NOT NULL,
    unit_key         text        NOT NULL,
    conversation_id  text        NOT NULL,
    day              date        NOT NULL,
    status           text        NOT NULL DEFAULT 'pending',
    cursor           text,
    pages_done       integer     NOT NULL DEFAULT 0,
    expected_count   integer,
    collected_count  integer     NOT NULL DEFAULT 0,
    recon_status     text        NOT NULL DEFAULT 'pending',
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_work_units PRIMARY KEY (job_id, unit_key),
    CONSTRAINT fk_work_units_tenant_id_job_id_collection_jobs
        FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    CONSTRAINT ck_work_units_status CHECK (status IN ('pending', 'running', 'done', 'failed')),
    CONSTRAINT ck_work_units_recon_status CHECK (recon_status IN (
        'pending', 'matched', 'gap', 'surplus', 'unverifiable', 'failed')),
    CONSTRAINT ck_work_units_counts CHECK (
        collected_count >= 0 AND pages_done >= 0 AND (expected_count IS NULL OR expected_count >= 0))
);
CREATE INDEX ix_work_units_job_id_status ON work_units (job_id, status);

CREATE VIEW checkpoints WITH (security_invoker = true) AS
    SELECT tenant_id, job_id, unit_key, cursor, pages_done, updated_at FROM work_units;
CREATE VIEW reconciliation WITH (security_invoker = true) AS
    SELECT tenant_id, job_id, unit_key, expected_count, collected_count, recon_status AS status
    FROM work_units;

-- ------------------------------------------------------------------ evidence registry (ADR 0002)
CREATE TABLE evidence_objects (
    id            uuid        NOT NULL,
    tenant_id     uuid        NOT NULL,
    job_id        uuid,
    storage_key   text        NOT NULL,
    kind          text        NOT NULL,
    state         text        NOT NULL DEFAULT 'pending',
    sha256        text,
    size_bytes    bigint,
    retain_until  timestamptz NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,
    CONSTRAINT pk_evidence_objects PRIMARY KEY (id),
    CONSTRAINT uq_evidence_objects_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_evidence_objects_storage_key UNIQUE (storage_key),
    CONSTRAINT fk_evidence_objects_tenant_id_job_id_collection_jobs
        FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    CONSTRAINT ck_evidence_objects_kind CHECK (kind IN ('page', 'file', 'seal', 'report')),
    CONSTRAINT ck_evidence_objects_state CHECK (state IN ('pending', 'complete', 'missing')),
    CONSTRAINT ck_evidence_objects_complete_has_hash CHECK (
        state <> 'complete' OR (sha256 {HEX64} AND size_bytes >= 0 AND completed_at IS NOT NULL))
);
CREATE INDEX ix_evidence_objects_job_id_state ON evidence_objects (job_id, state);

CREATE FUNCTION guard_evidence_objects() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state <> 'pending' THEN
        RAISE EXCEPTION 'evidence_objects: % row % is final', OLD.state, OLD.id USING ERRCODE = 'EA002';
    END IF;
    IF NEW.state NOT IN ('complete', 'missing')
       OR (NEW.id, NEW.tenant_id, NEW.job_id, NEW.storage_key, NEW.kind, NEW.retain_until, NEW.created_at)
          IS DISTINCT FROM
          (OLD.id, OLD.tenant_id, OLD.job_id, OLD.storage_key, OLD.kind, OLD.retain_until, OLD.created_at)
    THEN
        RAISE EXCEPTION 'evidence_objects: only pending -> complete|missing with hash/size is allowed'
            USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER trg_evidence_objects_guard BEFORE UPDATE ON evidence_objects
    FOR EACH ROW EXECUTE FUNCTION guard_evidence_objects();

-- ------------------------------------------------------------------ custody (ADR 0003)
CREATE TABLE custody_events (
    id          uuid        NOT NULL,
    tenant_id   uuid        NOT NULL,
    stream_id   uuid        NOT NULL,
    job_id      uuid,
    seq         bigint      NOT NULL,
    event_type  text        NOT NULL,
    actor       text        NOT NULL,
    item_id     uuid,
    payload     jsonb       NOT NULL,
    prev_hash   text        NOT NULL,
    event_hash  text        NOT NULL,
    created_at  timestamptz NOT NULL,
    CONSTRAINT pk_custody_events PRIMARY KEY (id),
    CONSTRAINT uq_custody_events_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_custody_events_stream_id_seq UNIQUE (stream_id, seq),
    CONSTRAINT fk_custody_events_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_custody_events_tenant_id_job_id_collection_jobs
        FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    CONSTRAINT ck_custody_events_seq_positive CHECK (seq >= 1),
    CONSTRAINT ck_custody_events_hashes CHECK (prev_hash {HEX64} AND event_hash {HEX64})
);
CREATE INDEX ix_custody_events_job_id ON custody_events (job_id);

CREATE TABLE custody_chain_heads (
    stream_id   uuid        NOT NULL,
    tenant_id   uuid        NOT NULL,
    last_seq    bigint      NOT NULL,
    last_hash   text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_custody_chain_heads PRIMARY KEY (stream_id),
    CONSTRAINT fk_custody_chain_heads_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT ck_custody_chain_heads_last_hash CHECK (last_hash {HEX64})
);

CREATE FUNCTION guard_custody_chain_heads() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.stream_id <> OLD.stream_id OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.last_seq <> OLD.last_seq + 1 THEN
        RAISE EXCEPTION 'custody_chain_heads: head may only advance by exactly one' USING ERRCODE = 'EA003';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER trg_custody_chain_heads_guard BEFORE UPDATE ON custody_chain_heads
    FOR EACH ROW EXECUTE FUNCTION guard_custody_chain_heads();

-- ------------------------------------------------------------------ items (append-only, ADR 0004)
CREATE TABLE items (
    id                  uuid        NOT NULL,
    tenant_id           uuid        NOT NULL,
    job_id              uuid        NOT NULL,
    source              text        NOT NULL,
    source_item_id      text        NOT NULL,
    version             integer     NOT NULL,
    item_type           text        NOT NULL,
    event_kind          text,
    content_hash        text        NOT NULL,
    raw_hash            text        NOT NULL,
    evidence_object_id  uuid        NOT NULL,
    storage_key         text        NOT NULL,
    json_path           text        NOT NULL,
    parent_item_id      uuid,
    change_hints        jsonb       NOT NULL DEFAULT '{{}}',
    sent_at             timestamptz,
    connector_version   text        NOT NULL,
    normalizer_version  text        NOT NULL,
    idempotency_key     text        NOT NULL,
    collected_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_items PRIMARY KEY (id),
    CONSTRAINT uq_items_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_items_tenant_id_idempotency_key UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT uq_items_tenant_id_source_source_item_id_version
        UNIQUE (tenant_id, source, source_item_id, version),
    CONSTRAINT fk_items_tenant_id_job_id_collection_jobs
        FOREIGN KEY (tenant_id, job_id) REFERENCES collection_jobs (tenant_id, id),
    CONSTRAINT fk_items_tenant_id_evidence_object_id_evidence_objects
        FOREIGN KEY (tenant_id, evidence_object_id) REFERENCES evidence_objects (tenant_id, id),
    CONSTRAINT fk_items_tenant_id_parent_item_id_items
        FOREIGN KEY (tenant_id, parent_item_id) REFERENCES items (tenant_id, id),
    CONSTRAINT ck_items_item_type CHECK (item_type IN ('message', 'file', 'event')),
    CONSTRAINT ck_items_event_kind CHECK (
        (item_type = 'event') = (event_kind IS NOT NULL)
        AND (event_kind IS NULL OR event_kind IN (
            'reaction_snapshot', 'identity_snapshot', 'change_observation'))),
    CONSTRAINT ck_items_version_positive CHECK (version >= 1),
    CONSTRAINT ck_items_hashes CHECK (
        content_hash {HEX64} AND raw_hash {HEX64} AND idempotency_key {HEX64})
);

CREATE TABLE job_items (
    tenant_id         uuid        NOT NULL,
    job_id            uuid        NOT NULL,
    item_id           uuid        NOT NULL,
    unit_key          text        NOT NULL,
    custody_event_id  uuid        NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_job_items PRIMARY KEY (job_id, item_id),
    CONSTRAINT fk_job_items_job_id_unit_key_work_units
        FOREIGN KEY (job_id, unit_key) REFERENCES work_units (job_id, unit_key),
    CONSTRAINT fk_job_items_tenant_id_item_id_items
        FOREIGN KEY (tenant_id, item_id) REFERENCES items (tenant_id, id),
    CONSTRAINT fk_job_items_tenant_id_custody_event_id_custody_events
        FOREIGN KEY (tenant_id, custody_event_id) REFERENCES custody_events (tenant_id, id)
);
CREATE INDEX ix_job_items_job_id_unit_key ON job_items (job_id, unit_key);
CREATE INDEX ix_job_items_custody_event_id ON job_items (custody_event_id);

-- ------------------------------------------------------------------ matters: retention only extends
CREATE FUNCTION guard_matters() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.retention_until < OLD.retention_until OR NEW.tenant_id <> OLD.tenant_id THEN
        RAISE EXCEPTION 'matters: retention_until may only be extended' USING ERRCODE = 'EA004';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER trg_matters_guard BEFORE UPDATE ON matters
    FOR EACH ROW EXECUTE FUNCTION guard_matters();

-- ------------------------------------------------------------------ tenant creation (narrow definer)
CREATE FUNCTION create_tenant(p_id uuid, p_name text, p_subdomain text, p_kms_key_ref text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = edisc, pg_temp AS $$
DECLARE
    previous text := current_setting('app.tenant_id', true);
BEGIN
    PERFORM set_config('app.tenant_id', p_id::text, true);
    INSERT INTO tenants (id, name, subdomain, kms_key_ref) VALUES (p_id, p_name, p_subdomain, p_kms_key_ref);
    PERFORM set_config('app.tenant_id', coalesce(previous, ''), true);
    RETURN p_id;
END
$$;
"""


def _rls_sql() -> str:
    parts = [
        "ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE tenants FORCE ROW LEVEL SECURITY;",
        "CREATE POLICY tenant_isolation ON tenants USING (id = current_tenant_id()) "
        "WITH CHECK (id = current_tenant_id());",
    ]
    for table in TENANT_TABLES:
        parts += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
            f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id = current_tenant_id()) "
            "WITH CHECK (tenant_id = current_tenant_id());",
        ]
    return "\n".join(parts)


def _append_only_sql() -> str:
    parts = []
    for table in APPEND_ONLY_TABLES:
        parts += [
            f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_mutation();",
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_mutation();",
        ]
    for table in APPEND_ONLY_TABLES + NO_DELETE_TABLES:
        if table not in APPEND_ONLY_TABLES:
            parts.append(
                f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_mutation();"
            )
        parts.append(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();"
        )
    return "\n".join(parts)


def _grants_sql(app_role: str) -> str:
    app = f'"{app_role}"'
    grants = {
        "SELECT": ["tenants", "checkpoints", "reconciliation"],
        "SELECT, INSERT": ["collection_scopes", "items", "job_items", "custody_events"],
        "SELECT, INSERT, UPDATE": [
            "matters",
            "connections",
            "custodians",
            "custodian_identities",
            "collection_jobs",
            "work_units",
            "evidence_objects",
            "custody_chain_heads",
        ],
    }
    parts = [
        f"REVOKE ALL ON ALL TABLES IN SCHEMA edisc FROM PUBLIC, {app};",
        f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA edisc FROM PUBLIC, {app};",
        f"GRANT USAGE ON SCHEMA edisc TO {app};",
    ]
    for privs, tables in grants.items():
        parts.append(f"GRANT {privs} ON {', '.join(tables)} TO {app};")
    parts += [
        f"GRANT EXECUTE ON FUNCTION current_tenant_id() TO {app};",
        f"GRANT EXECUTE ON FUNCTION create_tenant(uuid, text, text, text) TO {app};",
        # Belt and braces: nothing above grants these, but make the intent explicit.
        f"REVOKE TRUNCATE, DELETE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA edisc FROM {app};",
    ]
    return "\n".join(parts)


def _run(script: str) -> None:
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    app_role = op.get_context().config.attributes.get("app_role", "edisc_app")  # type: ignore[union-attr]
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    _run(UPGRADE_SQL)
    _run(_rls_sql())
    _run(_append_only_sql())
    _run(_grants_sql(app_role))


def downgrade() -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    _run(
        """
        DROP VIEW IF EXISTS reconciliation, checkpoints;
        DROP TABLE IF EXISTS job_items, items, custody_chain_heads, custody_events, evidence_objects,
            work_units, collection_scopes, collection_jobs, custodian_identities, custodians,
            connections, matters, tenants CASCADE;
        DROP FUNCTION IF EXISTS create_tenant(uuid, text, text, text), guard_matters(),
            guard_custody_chain_heads(), guard_evidence_objects(), reject_mutation(), current_tenant_id();
        """
    )
