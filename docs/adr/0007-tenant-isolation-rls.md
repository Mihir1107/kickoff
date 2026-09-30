# ADR 0007: Tenant isolation with Postgres row-level security

Status: Accepted (2026-09-30), implemented in migration 0001

## Decision
- Every tenant-scoped table (tenants, matters, connections, custodians, custodian_identities,
  collection_jobs, collection_scopes, work_units, evidence_objects, items, job_items, custody_events,
  custody_chain_heads) has `ENABLE` + **`FORCE ROW LEVEL SECURITY`** and one policy
  `tenant_isolation`: `USING (tenant_id = current_tenant_id()) WITH CHECK (same)` (`id` on tenants).
  The `checkpoints` and `reconciliation` views are `security_invoker`, so the same policies apply.
- `current_tenant_id()` = `NULLIF(current_setting('app.tenant_id', true), '')::uuid`. No context means
  NULL, which means zero rows visible and every insert rejected.
- **Roles** (created by `edisc_db.bootstrap` as superuser; everything else by Alembic as owner):
  - `edisc_owner`: owns the schema and all objects; runs migrations only (break-glass in production).
    Not superuser, no BYPASSRLS; FORCE RLS applies to it too.
  - `edisc_app`: API and workers. NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, NOINHERIT,
    not a member of the owner role, owns nothing, cannot create objects. Grants: SELECT/INSERT on
    append-only tables, SELECT/INSERT/UPDATE on mutable ones, SELECT on tenants and views. No DELETE and
    no TRUNCATE anywhere. Tenants are created only via the `create_tenant()` SECURITY DEFINER function.
- **Tenant context is transaction-local**: `edisc_db.session.tenant_tx()` runs
  `set_config('app.tenant_id', $1, true)`, the parameterizable form of `SET LOCAL`. Every API handler
  and every Temporal activity uses it; pooled connections cannot leak context.
- **Composite foreign keys** `(tenant_id, x_id) -> parent(tenant_id, id)` make cross-tenant references
  impossible even for code that sets the right context.
- **SECURITY DEFINER functions** (`create_tenant`, `due_anchor_streams`) pin
  `search_path = pg_catalog, edisc, pg_temp` (pg_temp last). PUBLIC never has EXECUTE:
  `create_tenant` is executable by `edisc_app`, and `due_anchor_streams` only by its owner, the
  `edisc_sweeper` login. The sweeper login can read nothing else in the schema. A test enumerates
  every definer function and asserts both properties.
- **Downgrades** are refused outside `EDISC_ENV` local/ci and always need an explicit target
  revision (fix-forward only in shared environments).
- **Tests** (`tests/integration/db`): through `edisc_app`, tenant A cannot read B's rows in any table or
  view, insert rows carrying B's id, update B's rows, or move its own rows to B. With no context it sees
  nothing. The app role cannot ALTER/DROP tables, disable or un-force RLS, drop or create policies,
  drop or disable triggers, replace security functions, set `session_replication_role`, turn off
  `row_security`, `SET ROLE` to the owner, create objects, TRUNCATE or DELETE.

## Residual risk
- A compromised app process can set any tenant id. RLS protects against *bugs* (missing WHERE clauses,
  wrong joins), not a hostile app server. Stronger options (per-tenant roles, signed context) are
  backlog items.
- The owner and superuser can disable triggers and RLS. That tampering is what the custody chain,
  Merkle roots and WORM seals detect (ADR 0003).
