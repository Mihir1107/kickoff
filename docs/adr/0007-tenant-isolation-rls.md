# ADR 0007: Tenant isolation with Postgres row-level security

Status: Accepted (2026-09-30)

## Decision
- Every tenant-scoped table has `tenant_id` and `ENABLE` + **`FORCE ROW LEVEL SECURITY`**, with policies
  `USING (tenant_id = current_setting('app.tenant_id')::uuid)` and the same `WITH CHECK`.
- Tables are owned by a migration role `edisc_owner`. The application and workers connect as
  **`edisc_app`**, which is neither owner nor superuser and has no `BYPASSRLS`.
- Tenant context is set with `SET LOCAL app.tenant_id = …` **per transaction**, including inside every
  Temporal activity. Unset context ⇒ `current_setting(..., true)` is NULL ⇒ zero rows visible/writable.
- Test: through `edisc_app`, tenant A can neither read nor write tenant B's rows (select, insert with
  B's id, update, and custody append all fail or see nothing).
- `tenants` itself is readable only for the current tenant; tenant creation goes through a narrow
  SECURITY DEFINER function.

## Consequences
- + Isolation holds even if application code forgets a `WHERE tenant_id`.
- − Every DB session helper must set context; enforced by a single `tenant_tx()` helper.
