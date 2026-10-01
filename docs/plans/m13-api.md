# M13 plan: collection API (FastAPI)

Depends on ADR 0013 (proposed: authn/authz). Nothing here is implemented yet. Milestones run in order;
each one is implement → tests → run → commit.

## Requirements carried into every milestone
1. The tenant comes from the request's subdomain AND the authenticated user (ADR 0013 §2). There is no
   `tenant_id` in any request body or query. A test sends a forged `tenant_id` everywhere and asserts it
   is ignored or rejected.
2. Every state-changing call (connect, reauth, start, cancel, resume, rerun) writes a custody event (job
   actions) or an audit event (everything else) with the acting user or service principal.
3. `POST /jobs` accepts an `Idempotency-Key` header. A retried POST never starts two jobs.
4. Real multi-scope jobs replace the single-scope guard, including thread-context ranges per scope.
5. Every list endpoint uses cursor pagination. Tokens and secrets never appear in any response.
6. The auth model follows ADR 0013 once it is accepted. Nothing auth-related is implemented before that.

## M13.1: hierarchy and principals (after ADR 0013 is accepted)
- Migration: `clients`, `workspaces`, `matters.client_id` (backfilled with a default client per tenant),
  `users`, `groups`, `group_mappings`, `service_accounts`, `role_assignments`, `tenant_idps`,
  `audit_events` (append-only, hash-chained via `edisc_custody` with stream = tenant). All tenant-scoped,
  FORCE RLS, grants, model drift test.
- Tests:
  - the RLS tenant A/B test for each new table;
  - `audit_events` append-only (UPDATE/DELETE/TRUNCATE rejected);
  - the backfill keeps every existing matter reachable.

## M13.2: authentication and tenant resolution
- `edisc_api.auth`: tenant resolved from `Host`; OIDC JWT validation (JWKS cache with rotation,
  iss/aud/exp/nbf, skew); principal lookup `(tenant, iss, sub)`; dev issuer only in local/test/ci.
- Tests, all through the real API with httpx:
  - token for tenant A on tenant B's host → 404;
  - wrong `aud`, expired, `alg=none` and unknown `kid` → 401;
  - deactivated user → 401;
  - forged `tenant_id` fields are ignored;
  - the dev issuer is refused when `EDISC_ENV=production`.

## M13.3: authorization
- `require(permission, scope)` dependency, role → permission table in code, effective-permission query
  with downward inheritance.
- Tests:
  - an access matrix: each role × each route × in-scope / out-of-scope, generated from the route table;
  - a test that fails if any route lacks a declared permission;
  - 404 vs 403 behaviour;
  - group-claim mapping.

## M13.4: connections
- Routes:
  - `POST /clients/{c}/connections` (dummy now): validate, store tokens via `edisc_db.connection_tokens`,
    audit event;
  - `POST .../{id}/reauth`: replaces tokens, `resume_connection` for paused jobs, signals `wake` to their
    workflows, audit event;
  - `GET` list/detail with cursor pagination.
- Responses expose status, scopes and blind spots only.
- Tests:
  - secrets never appear in any response (scanner over every response in the suite) or in logs;
  - reauth resumes paused jobs end to end (Temporal).

## M13.5: multi-scope jobs (replaces the guard)
- **Design** (short ADR 0011/0005 amendment before coding):
  - A job has N scopes, each with its own date range, custodian/channel selector and thread-parent policy
    (ADR 0011).
  - **Units are the union over scopes.** A work unit (conversation × day) records the scope ids that
    cover it (`work_unit_scopes`). It is enumerated once and fetched once.
  - **Ranges per scope.** `in_scope` on a job link is true if ANY covering scope's range contains the
    message. Thread context is resolved per covering scope, with that scope's policy and range. A parent
    outside every covering scope's range is fetched as context once, deduplicated by idempotency key.
  - **Reconciliation per unit.** Expected counts come for the unit's day. Absence detection runs only for
    units whose covering scopes all ended clean (ADR 0005 rule unchanged).
  - The custody `job_started` payload lists every scope (already the case).
- Tests:
  - two overlapping scopes (channel + custodian) produce each unit once and match the oracle;
  - different thread policies per scope get the per-scope context;
  - a reply in scope A whose parent lies in scope B's range is not duplicated;
  - the single-scope tests stay green.

## M13.6: jobs API
- Routes:
  - `POST /matters/{m}/jobs` with `Idempotency-Key`;
  - `GET /matters/{m}/jobs` (cursor);
  - `GET /jobs/{id}` (status, units summary, paused time, `completed_unverified` never shown as clean);
  - `POST /jobs/{id}/cancel`, `/resume`, `/rerun`;
  - `GET /jobs/{id}/units` (cursor), `/reconciliation`, `/custody/verify`.
- **Idempotency:**
  - Stored in `api_idempotency(tenant, principal, key, request_hash, job_id, created_at)`, unique
    `(tenant, key)`.
  - Same key + same body returns the original 201 response.
  - Same key + different body → 422.
  - Concurrent duplicates: one wins via the unique constraint; the other waits on the row and returns the
    winner's response.
  - The Temporal start uses `id = job_id` with REJECT_DUPLICATE as the second wall.
- Actions write custody events with `actor = user:{id}` plus the request id and idempotency key.
- Tests:
  - 20 concurrent identical POSTs → exactly one job and one workflow;
  - retry after a lost response returns the same job;
  - key reuse with a different body → 422;
  - cancel/resume/rerun end to end with custody actors;
  - cursor pagination is stable under concurrent inserts (no skips or duplicates);
  - out-of-scope users are denied.

## M13.7: hardening
- OpenAPI schema test; response-model coverage (no route returns a raw dict); rate limiting on auth
  failures; request ids in logs and custody payloads; `docs/ARCHITECTURE.md` + CLAUDE.md updates.

## Open decisions before starting
- ADR 0013's open questions (connection ownership, role list, workspaces now, read auditing).
- The storage/throughput options from docs/runs/2026-10-01-storage-throughput-breakdown.md: which to do
  before M13, if any.
