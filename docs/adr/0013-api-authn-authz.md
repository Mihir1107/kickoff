# ADR 0013: API authentication and authorization (tenant > client > matter > workspace)

Status: **Proposed** (2026-10-01). Not implemented. M13 builds on it once it is accepted.

## Context
- **What M13 exposes:** the collection module over HTTP. Callers connect sources, start, cancel, resume and
  rerun jobs, and read status, reconciliation and custody.
- **Today's model:**
  - Tenant isolation is enforced in Postgres with FORCE RLS and `SET LOCAL app.tenant_id` (ADR 0007).
  - Everything below the tenant is flat: tenant > matter.
  - The product follows the Relativity model: instance > client > matter > workspace, with permissions
    granted to groups at each level.

**Requirements:**
- The tenant comes from the request's subdomain AND the authenticated user. It never comes from a
  client-supplied `tenant_id` field.
- Every state-changing call records the acting user in custody or audit.
- Tokens and secrets never appear in responses.

## Decision

### 1. Hierarchy
New tables, each tenant-scoped with FORCE RLS like the rest:

| Level | Table | Owns |
|---|---|---|
| Tenant | `tenants` (exists) | users, groups, IdP configuration, clients |
| Client | `clients` (new) | matters; **source connections** (the client's Slack/Teams org belongs to the client, reused across its matters) |
| Matter | `matters` (+ `client_id`) | collection jobs, retention (`retention_until`), custodians in scope |
| Workspace | `workspaces` (new, `matter_id`) | the review destination a job's output is delivered to (later phases); a job may name one |

Existing matters migrate to one default client per tenant, so no data is lost.

### 2. Authentication (who is calling)
- **Users: OIDC bearer tokens from the tenant's own IdP** (Entra ID, Okta, ...).
  - Each tenant registers issuer, audience and JWKS URL in `tenant_idps`.
  - The API validates the signature (JWKS cached, key rotation honoured), `iss`, `aud`, `exp`/`nbf`
    (60 s skew) and the presence of `sub`.
  - Principle 6 is about source tokens. The customer's own IdP is not a third-party broker, and we hold
    no user passwords.
- **Tenant resolution:**
  1. The tenant is resolved from the `Host` subdomain (`{subdomain}.app...`).
  2. The token's issuer must be one of THAT tenant's IdPs.
  3. The user `(tenant_id, iss, sub)` must exist and be active.
  - Any mismatch gives a 404 for the tenant (no enumeration). There is no `tenant_id` in any request body
    or query.
  - The resolved tenant goes into `tenant_tx`, so RLS is the second wall.
- **Service accounts** (automation): OAuth client-credentials tokens from the same IdP. They map to a
  service principal with explicit role assignments; they never inherit a user's.
- **Local / test only:** a built-in signing key issues dev tokens. It is refused outside `EDISC_ENV`
  local/test/ci (same pattern as the retention override).

### 3. Authorization (what they may do)
- **Scoped RBAC:** `role_assignments(principal, role, scope_type, scope_id)`.
  - The principal is a user, a group or a service account.
  - Scope types are tenant, client, matter and workspace.
  - Assignments inherit downward: a matter-level role applies to that matter's workspaces.
  - Groups come from an IdP claim (`groups`) mapped through `group_mappings`, or are managed locally.
- **Roles are fixed sets of permissions, defined in code** (versioned, reviewed). Tenants assign roles;
  they don't edit permission lists. Proposed:

| Role | Typical scope | Permissions |
|---|---|---|
| `tenant_admin` | tenant | everything below, plus IdP / users / groups / role assignments |
| `client_admin` | client | `connection.create/reauth/disable`, `matter.create`, all matter permissions |
| `matter_manager` | matter | `job.start/cancel/resume/rerun`, `custodian.manage`, read all |
| `collector` | matter | `job.start/cancel`, read jobs |
| `reviewer` | matter / workspace | read jobs, reconciliation, reports |
| `auditor` | tenant / client / matter | read jobs, custody chain, verification results, audit log (read-only, cross-matter) |

- **Enforcement:**
  - Every route declares `require(permission, scope_from_path)`.
  - The dependency loads the caller's effective permissions for that scope: one query, cached for the
    request.
  - A denied request returns 403 when the object is visible to the caller and 404 when it isn't.
  - Matter-level checks are application-level. RLS stays at the tenant boundary. Pushing matter scoping
    into RLS is a later option, if tenants demand defense in depth.
- **Separation of duties:**
  - No role may modify custody or evidence. No such API exists.
  - Re-authorization of a connection (`connection.reauth`) is distinct from `connection.create`, so it
    can be delegated.

### 4. Audit
- **Job-scoped actions** (start, cancel, resume, rerun) are custody events in the job's stream.
  - The `actor` is `user:{tenant_user_id}` or `service:{id}`.
  - The payload carries the request id and the Idempotency-Key.
- **Everything else that changes state** (connect, reauth, matter/client/workspace changes, role
  assignments, IdP changes) goes to an **append-only `audit_events` hash chain per tenant**.
  - It reuses the custody chain machinery with stream id = tenant id, so it is anchored to WORM like
    custody.
  - Login/token failures are logged (rate-limited), not chained.
- Reads are not chained in Phase 1 (access logging goes in the backlog).

### 5. Response hygiene
- Response models are explicit Pydantic schemas: never ORM rows or dicts.
- Connections expose status, scopes and blind spots, never token fields.
- A test scans every response of the OpenAPI-driven test suite for registered secrets and token-shaped
  strings.

## Consequences
- Plus: the tenant is never client-controlled. Two independent checks (host + IdP issuer) and RLS
  underneath.
- Plus: it matches how eDiscovery teams already delegate (Relativity groups per client/matter), so
  customers can map their groups directly.
- Plus: the custody chain names the human or service behind every job action.
- Minus: per-tenant IdP setup is an onboarding step. Local/test use dev tokens.
- Minus: authorization below the tenant is application code, so every route needs a declared permission.
  A test enumerates the routes and fails on any route without one.

## Open questions for review
1. **Connections at client level.** Is reusing one Slack/Teams org across a client's matters right, or
   must a connection be granted per matter? (Proposed: client-owned, with a matter-level `connection.use`
   permission.)
2. **Role list.** Is it enough for Phase 1, or should we start with tenant-defined custom roles?
   (Proposed: fixed roles now; custom roles go in the backlog.)
3. **Workspaces in M13.** Model them now and leave them unused by jobs, or defer the table to the
   export/RSMF phase? (Proposed: model now, so permissions and URLs are stable.)
4. **Audit of reads** (who viewed which custody or report): Phase 1 or backlog? (Proposed: backlog.)
