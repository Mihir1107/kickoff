# Phase 2 plan: Slack export ingestion, RSMF, preview and report, minimal UI (proposal)

Status: **proposed, for review.** Nothing in this document is implemented. Each milestone runs
implement → tests → run → commit, starting with an ADR where marked.

**Prerequisite (recommended first, small):** anchor coalescing (docs/runs/2026-10-01-audit-burst.md, option A).
- Every phase-2 milestone adds custody and audit events: render, report and download.
- The current anchor storm writes one WORM anchor per ~2 events under concurrency instead of one per 8.
- It should be fixed before volume grows.

---

## M14: Slack export ingestion connector (`edisc_connector_slack_export`)
An admin uploads the standard Slack export zip. The zip is evidence first, and its entries then flow
through the existing pipeline.

**ADR 0014 (before code)** covers:

**1. Upload and lock first.**
- `POST /v1/clients/{c}/exports` takes a resumable chunked upload, so multi-GB zips work through proxies.
- The bytes stream straight into the existing evidence writer: hashed while streaming, Object Lock set at
  creation, nothing on worker disk.
- The zip's SHA-256 and size are returned and recorded in an `audit.export_uploaded` event before any
  processing.
- An "export" is a connection-like source owned by the client (decision a). A job references it like a
  connection.

**2. Reading the zip from WORM without local disk.**
- A seekable reader over the pinned object version uses S3 range GETs (the central directory is at the end).
- Each entry is streamed and its CRC-32 is checked against the central directory.
- The entry's own SHA-256 is computed while it is read.
- A corrupt or truncated archive is a job-scoped integrity failure.

**3. Provenance of entries.** Proposed: no second copy.
- Items point at a new evidence kind `archive_entry`: the zip's evidence id, the entry path and the entry
  SHA-256, plus `json_path` inside the entry.
- `edisc-verify` re-reads the entry from the pinned zip and checks its hash.
- Alternative: write every day file as its own page object. That is simpler for the verifier but doubles
  storage. **Decision needed.**

**4. Units and reconciliation.**
- Units are `channels.json`, `groups.json` (private channels), `dms.json` and `mpims.json` × the
  per-day files (`<channel>/<YYYY-MM-DD>.json`), filtered by the job's scopes.
- An export has no server-side counts, so the source's expected count is unavailable. Units would end
  `completed_unverified` (ADR 0005), which is correct but not useful.
- Proposed **archive completeness** instead:
  - every central-directory entry is accounted for (processed, out of scope, or listed as unknown);
  - every listed channel has its directory;
  - every day file parses;
  - per-file message count = items produced.
- A unit is then `matched` against the export's own content, and the report states that the export is
  the system of record ("verified against archive", not against Slack). **Decision needed:** new recon
  status `archive_matched`, or reuse `matched` with a report note.

**5. Business+ / Enterprise exports.**
- Private channels (`groups.json`), DMs (`dms.json`) and group DMs (`mpims.json`).
- Shared channels and external users.
- `users.json` becomes identity snapshots (the existing directory unit path).
- Deleted users, bot and app messages, channel join/leave/topic events, edited and deleted messages, and
  threads across days are handled.
- Canvases, lists and huddle transcripts are listed as blind spots unless present.

**6. Files.**
- Export messages carry `url_private_download`; on Business+ exports these include a token parameter.
- These URLs are **secrets**: registered for redaction, never stored in derived data, never logged.
- Downloads go through the rate limiter (`slack_export.file` bucket) and the existing small/large file paths.
- Missing or expired files are recorded as file gaps, as today.

**7. Normalizer.**
- Export messages are close to `conversations.history`. A `slack_export` dialect is added to the dummy
  generator so the oracle tests run on export-shaped data too.
- The Slack normalizer gets export-specific parsing (embedded `user_profile`, file stubs, channel events)
  behind the same fingerprint versions where the meaning is the same. A new fingerprint version is used
  where it is not.

**Tests:**
- Oracle-exact on dummy export zips.
- Corrupt or truncated zips, a CRC mismatch, entries outside any scope, path traversal entries (`../`),
  zip bombs (ratio and size caps), duplicate entry names, and 5 GB streaming with bounded memory.
- SIGKILL during ingestion resumes exactly (the existing crash matrix).
- **A real export from a Slack Developer Program sandbox:**
  - Business+/Enterprise features: private channels, DMs, threads, edits, deletions, files.
  - The sanitised export is stored as a CI fixture, or in a private bucket if too large, plus a
    hand-checked expected summary (counts per channel-day, thread structure).
  - **Needs from you:** a sandbox workspace and an exported zip. I cannot create the Slack account
    myself.

---

## M15: RSMF renderer (`edisc_renderers.rsmf`)

**Detailed design: `docs/adr/0015-rsmf-renderer.md` (accepted 2026-10-03 with review decisions; not
implemented).** It supersedes the outline below where they differ:
- a render custody stream of its own, starting from the sealed job's head and seal anchor;
- group DMs as `direct` with the Slack type in `custom`;
- `include_context` as a recorded render option;
- `X-RSMF-RendererVersion`.

**Implementation order once approved:**
1. Vendor the schema (BSD-3 `LICENSE`, `SOURCE.md` with commit and SHA-256) and add `jsonschema` as a
   test and render-time validator.
2. The pure renderer: slicing (UTC, matter time zone, DST), the 10,000-event cap with parts, mapping,
   attachments and placeholders, the deterministic zip and EML; golden bytes.
3. The loader from items and derivations, and storage as `production` registry rows.
4. `RenderWorkflow`, the render custody stream, the API with recent sign-in, audited downloads.
5. The fixture corpus (both dialects), structural EML checks, the crash matrix for renders.

**ADR 0015** covers:

**1. Input and slicing.**
- Input: derived items (latest normalizer version) plus file evidence, never raw pages directly.
- Slices are one conversation per 24 h by default. UTC by default; a matter time zone is optional and uses
  `local_day_bounds`, including DST.
- Cap: 10,000 events per file; above that the slice is split into `part N of M`.
- Threads stay readable: a reply in a slice whose parent is in an earlier slice carries the parent as
  context (marked), following ADR 0011.

**2. Output.**
- An `.eml` with `X-RSMF-Version` set to the pinned version, `X-RSMF-Generator`, `X-RSMF-BeginDate`,
  `X-RSMF-EndDate`, `X-RSMF-EventCount`.
- Custom headers:
  - `X-RSMF-CollectionId` (job id);
  - `X-RSMF-SourceHash`: Merkle root over (idempotency key, content hash) of the slice's items, using the
    same RFC 6962 construction as custody, so it can be checked against the chain;
  - `X-RSMF-ConnectorVersion` and `X-RSMF-NormalizerVersion`.
- The attachment `rsmf.zip` contains `rsmf_manifest.json` (participants, conversations, events with
  parent/thread, reactions, edits, deletions, attachments by reference), the attachment files and,
  optionally, avatars.
- Unavailable files appear as placeholder attachments with the reason.

**3. Determinism.** The same inputs give byte-identical output: fixed zip timestamps and ordering, and
canonical JSON where the schema allows. A golden test compares the bytes.

**4. Storage and custody.**
- Renders are derived products, not raw evidence: `t/{tenant}/productions/{job}/...`.
- They are hashed while written and locked for the matter window.
- Each render is a `report_generated`-style lifecycle custody event (`rsmf_rendered`) listing every file
  with its hash.

**5. Validation in CI.**
- Manifests are validated against Relativity's published RSMF JSON schema, pinned and vendored with its
  version and source URL. The EML is checked structurally.
- **Proposed:** also run Relativity's RSMF validator in CI if its licence allows it (it is a .NET tool;
  the CI job would add the .NET runtime). **Decision needed.**
- Fixture corpus: edge cases (emoji/RTL/zero-width, very long messages, attachment-only, deleted with
  tombstones, edits, cross-day threads, the 10k split) rendered and validated.

**API:**
- `POST /v1/jobs/{id}/renders` (permission `export.create`: matter_manager or reviewer?
  **Decision needed**).
- `GET .../renders/{id}`, and downloads through the audited content path (purpose `rsmf`).

---

## M16: HTML preview and the collection report

**1. Preview.**
- Renders from the same intermediate as RSMF: a conversation-day view with threads, edits, deletions,
  reactions and attachments.
- Static, sanitised HTML: everything escaped, no scripts, no external resources, strict CSP.
- Attachments link to the audited content endpoint (purpose `preview`). Served by the API to permitted
  roles only.

**2. Collection report** (HTML + JSON, plus PDF?, **decision needed**), deterministic and hashed. Contents:
- scopes (ranges, policies) and the access tier, plan and granted scopes;
- **blind spots** (from `validate_connection` and per-source lists);
- counts per unit;
- gaps, unverifiable and failed units, with reasons;
- file unavailability;
- access-lost observations and no-longer-observed events;
- pauses (who re-authorized, how long);
- connector and normalizer versions;
- operator/actor for every action;
- the **custody verification result** (events, batches, items, anchors and Merkle roots checked, seal);
- the evidence store's lock settings.

**3. Reporting rules.**
- `completed_unverified` and `completed_with_failed_units` are never rendered as success.
- The report is a lifecycle custody event `report_generated`. The report's hash is included, so a later
  change to the report is detectable.

---

## M17: minimal UI (Next.js)
Pages: connections (list, create, re-authorize; export upload), create collection (matter, connection,
scopes form with ranges and policies), job status (live; units; pauses; cancel/resume/rerun), report
view, downloads (RSMF and report, through the audited endpoints).

**ADR 0016 (before code)** covers:

**1. Auth model.** Proposed: a **BFF** (backend-for-frontend).
- The Next.js server does OIDC (authorization code + PKCE) with the tenant's IdP and keeps tokens
  server-side in an encrypted, httpOnly, SameSite=strict session. The browser never sees a token.
- The alternative is a pure SPA holding tokens in memory: simpler, weaker. **Decision needed.**

**2. Tenant from the host.** The same subdomain scheme as the API, so the UI can't pick a tenant either.

**3. Security.**
- Strict CSP, no inline scripts and CSRF protection on BFF mutations.
- Downloads stream through the BFF and keep the audit purpose.

**4. Testing.** Playwright end to end against the live stack: dummy connection → collection → status →
report → download, with the audit trail checked. Plus accessibility checks (axe).

Out of scope for M17 (backlog): admin screens for IdPs and roles (API-only for now), theming, i18n.

### M17 backend (PROPOSED 2026-10-03, amended after review; not implemented)
The frontend now exists as a Vite + React single-page app (`apps/web`, built elsewhere, on its own
branch), not Next.js. So there is no separate BFF server: **the API itself holds the session**, which
keeps decision 7 (server-side sessions, httpOnly cookies, CSRF, revocation, no tokens in the browser).
ADR 0016 (`docs/adr/0016-session-auth.md`, accepted 2026-10-03) fixes the contracts: `return_to` and
`reauth=1` on login, `{csrf_token}`, `{authorize_url, connection_id}` and `{token}`.

**1. Session auth (ADR 0016).**
- **Login:** `GET /v1/auth/login` starts OIDC authorization code + PKCE with the tenant's IdP (tenant from
  the Host subdomain, as everywhere). `GET /v1/auth/callback` exchanges the code server-side, checks
  state, nonce and audience, resolves the active principal (same rules as bearer auth), and creates a
  session. The IdP tokens are used once and never stored or sent to the browser. A dev login exists only
  where the dev IdP is allowed (local/test/ci).
- **Cookie:** `__Host-edisc_session`: random 256-bit id, httpOnly, Secure, SameSite=Strict, Path=/, no
  Domain attribute. The `__Host-` prefix makes it host-only, so it is scoped to exactly the tenant
  subdomain and never sent to another tenant. The short-lived login state cookie (PKCE verifier, state)
  is `__Host-edisc_login`, SameSite=Lax, because the IdP's redirect back is a cross-site navigation that
  a Strict cookie would not survive. It is deleted at the callback.
- **Server-side sessions:** a `sessions` table (RLS) storing the SHA-256 of the id, never the id itself,
  plus tenant, principal, created, last seen, absolute expiry (e.g. 12 h) and idle expiry (e.g. 30 min),
  revoked at/by, and user agent. Every request checks the row. Expiry and revocation take effect at once.
- **CSRF:** `GET /v1/auth/csrf` returns a token bound to the session (HMAC of the session id with a
  server key). Every state-changing request (POST/PUT/PATCH/DELETE) authenticated by the cookie must send
  it in `X-CSRF-Token`, and its `Origin` must be the tenant host; otherwise 403. Bearer-token callers
  (service principals, scripts) are exempt, because no ambient credential is involved.
- **Logout and revocation:** `POST /v1/auth/logout` revokes the current session and clears the cookie.
  `GET /v1/me/sessions` and `POST /v1/me/sessions/{id}/revoke` let a user see and end their own sessions.
  Tenant admins get `POST /v1/principals/{id}/sessions/revoke`, and deactivating a principal revokes all
  of its sessions. All of these are audited.
- **Session fixation:** a fresh session id is issued at every successful login (and at
  re-authentication). Any id the browser held before is never promoted to an authenticated session.
- **Recent sign-in for sensitive actions:**
  - Covered actions: close and reopen (matters, clients), export limit overrides, role assignment
    changes, and session revocation.
  - These need an authentication no older than `EDISC_API_REAUTH_MAX_AGE_SECONDS` (e.g. 10 min,
    recorded on the session as `authenticated_at`).
  - Otherwise the API answers 401 `reauth_required`, and the UI sends the user through the IdP again
    (`prompt=login`, `max_age`).
  - The audit event records the authentication time.
- **The caller dependency** accepts either a bearer token or a session cookie. A request carrying both
  is rejected (400) before either is evaluated, so a stray cookie can never stand in for a token or the
  reverse. Auth-failure throttling applies to the login and callback endpoints too.

**2. Permissions for the UI.**
- `GET /v1/me/permissions`: the caller's effective permissions per scope (tenant, and every client,
  matter and workspace where they hold a role), computed by `authz.py` itself.
- `GET /v1/roles`: the role matrix (role → permissions), generated from `ROLE_PERMISSIONS`. It is never
  hand-written, so the UI cannot drift from what the API enforces. The UI uses both only to show or hide
  controls; the API still authorizes every call.

**3. Missing endpoints.**
- `GET /v1/jobs`: tenant-wide, cursor-paginated, limited to what the caller can see (visible matters).
  Filters: status, clean basis, client, matter, connection, source, created/finished range.
- `GET /v1/jobs/{id}/custody/events`: the job's custody events by seq (cursor), with type, actor, time,
  hashes and payload. Payloads never carry secrets (they never did), so nothing extra needs redacting;
  needs `custody.read`.
- `GET /v1/connections/{id}/directory?kind=channels|custodians`: what the job wizard's scope picker
  needs. It **serves a stored snapshot and never calls the source on a request**.
  - A background job (maintenance schedule, plus one run when a connection is created or
    re-authorized) refreshes the snapshot through the rate limiter, via a new connector method.
    Live connectors list conversations and users; the export connector reads `export_conversations`
    and `users.json` once.
  - The response carries the snapshot's capture time (`captured_at`) and whether a refresh is running.
    An empty snapshot says "not captured yet", never "no channels".
  - Cursor-paginated; needs `job.start` on a matter of the client, or `connection.read`.
- `GET /v1/groups` (and `GET /v1/groups/{id}/members`): `tenant.admin`, paginated.

**4. Live updates: Server-Sent Events.**
- `GET /v1/jobs/{id}/stream` (`text/event-stream`, needs `job.read`, cookie or bearer).
  - Events: `job` (status, clean, clean basis, caveat, unit counts by status and recon status) and
    `unit` (units that changed).
  - Each event has an id. A reconnect with `Last-Event-ID` resumes with a fresh snapshot.
  - Heartbeat comments every 15 s; the stream closes after the job is sealed.
- **One source per job, fanned out:** one poll per job per API process (about once a second, only while
  it has subscribers), or Postgres LISTEN/NOTIFY published by the pipeline. Each change is fanned out to
  every subscriber of that job, so 100 open browser tabs cost one query a second, not 100. The wire
  format does not depend on which of the two is used.
- **Re-checked continuously:** at every heartbeat the stream re-validates its session (not expired, not
  revoked, principal active) and the caller's `job.read` on the job's matter. On revocation or a lost
  permission it sends a final `revoked` event and closes.
- **No proxy buffering:** `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`,
  `Connection: keep-alive`, and no compression on the stream.
- Limits: a cap on concurrent streams per principal (429 above it) and a maximum stream lifetime, after
  which the client reconnects.
- **Fallback:** if the stream fails or a proxy buffers it, the client polls `GET /v1/jobs/{id}` (and
  `/units`) every 5 s. The UI behaves the same either way.

**5. A stable OpenAPI spec.**
- `make openapi` writes `docs/api/openapi.json` from the app (sorted, deterministic) and it is committed.
- CI fails if the committed spec differs from the generated one, so every API change shows up in review.
- CI also enforces **additive-only** changes: a breaking-change diff tool (e.g. `oasdiff breaking`)
  compares the generated spec with the committed one on the base branch and fails on any breaking change
  (removed path, operation or field, new required field, narrowed type or enum). An intended break needs
  `/v2` or an explicit, reviewed allow-list entry.
- Every route gets a stable `operationId`. The spec carries `x-permission` per route (already present)
  and the error schema.
- Version rule: within `/v1`, only additive changes. A removal or a change of meaning needs `/v2` or a
  deprecation period.
- The frontend generates its types from this file (e.g. `openapi-typescript`) instead of the
  hand-mirrored `src/api/types.ts`, in the frontend's branch.

**6. Server-side install flows (no tokens in the browser).**
- **Slack (our distributed app): OAuth v2.**
  - `POST /v1/clients/{c}/connections/slack/install` (`connection.manage`) creates a pending install
    with a random `state` bound to the tenant, the client, the acting session and an expiry. It
    answers with the Slack authorize URL (scopes per tier, PKCE where Slack supports it).
  - The browser only follows redirects. Slack redirects to `GET /v1/oauth/slack/callback` on the
    tenant host. The API checks `state` (single use, unexpired, same session), exchanges the `code`
    server-side (`oauth.v2.access`) and stores the tokens directly through `connection_tokens`
    (envelope-encrypted).
  - The connection is created with granted scopes, team id and blind spots, then the API redirects to
    the UI's connection page. Tokens never appear in a response, a redirect URL, a log or Temporal.
  - Re-authorization uses the same flow. Failures, denials and replays are audited without values.
- **Microsoft Teams: admin consent.**
  - `POST /v1/clients/{c}/connections/teams/consent` (`connection.manage`) starts the tenant-admin
    consent URL for our multi-tenant app, with the same `state` binding.
  - The callback `GET /v1/oauth/microsoft/callback` receives `admin_consent` and the customer's
    Entra tenant id, verifies `state`, and records the connection.
  - Graph tokens are then obtained server-side with the client-credentials flow and a certificate
    (ADR 0009 / backlog), never from the browser.
  - A refused or partial consent is recorded as such, with the granted permissions listed.
- **Common to both:** callbacks are exempt from CSRF tokens (top-level GETs from the provider) but
  protected by the single-use, session-bound `state`. The redirect URIs are exact and registered per
  environment. The provider's error parameters are recorded, never reflected unescaped.

**7. Slack internal-app tier exception (ADR before code).** Some customers install their own internal
Slack app and hand us its token; there is no OAuth flow to run. One controlled exception:
- `POST /v1/clients/{c}/connections/slack/token` (`connection.manage`) accepts the token once (and
  `PUT /v1/connections/{id}/token` to replace it).
  - Write-only: no endpoint ever returns it, not even masked beyond a fixed "set at <time>".
  - It is validated against Slack (`auth.test`) and encrypted on receipt (`connection_tokens`); the
    plaintext lives only in that request's memory.
  - It is registered with the log redactor before any processing, and excluded from logs, error
    details, audit and custody payloads, and Temporal.
  - The audited connection event (`connection_token_submitted` / `_replaced`) records who did it,
    when, the team id, the token type and the granted scopes: never the value or any part of it.
- Tests:
  - every response of the API test suite is scanned for the submitted token (existing credential scan);
  - log capture contains no fragment of it;
  - the custody and audit payloads never contain it;
  - GETs on the connection never expose it.

**8. End-to-end tests in M17.** Playwright runs the UI against the REAL API and stack (the ephemeral
test stack, dev IdP), not mocks.
- Flows: sign-in → connection (dummy) → collection → live status (SSE, and the polling fallback) →
  report → audited download; export upload → validation findings → export job →
  `completed_against_archive` with its caveat; reopen/close; revocation closing an open stream.
- Accessibility: axe checks on every page.
- A **pixel contrast audit:** screenshots of each page in the **dark theme** (the only theme in M17; a
  light theme comes later, and the audit then covers both). Contrast is computed from the rendered
  pixels against WCAG AA: 4.5:1 for body text, 3:1 for large text and UI components (borders, icons,
  focus rings). Failures list the element and its measured ratio.
- Runs in CI against the compose stack; traces and screenshots are kept on failure.

**Tests.**
- Session lifecycle: login, idle and absolute expiry, logout, admin revocation, deactivation. The cookie
  carries the right flags and never reaches another tenant's subdomain.
- The session id changes at login (a pre-set id is never accepted). Sensitive actions are refused with
  an old authentication and allowed after re-authentication. Cookie plus bearer on one request is a 400.
- CSRF refused without the token, with a token from another session, or with a foreign Origin; bearer
  requests unaffected. No token or session id appears in any response body (the existing credential scan).
- `/me/permissions` and `/roles` derived from `authz.py`: a test changes a role and the endpoint follows.
- Jobs list visibility per role.
- SSE: snapshot, then changes, resume with `Last-Event-ID`, the per-principal cap, closing after the
  seal, and the polling fallback. N subscribers to one job cause one DB poll. A revoked session or a
  removed role closes an open stream at the next heartbeat. The no-buffering headers are present.
- Directory: requests never reach the source (connector call counter), `captured_at` is returned, and
  the refresh goes through the limiter.
- The OpenAPI drift check.

---

## Decisions (2026-10-02)
1. **Export entries:** referenced inside the locked zip by (zip evidence id, pinned VersionId, entry path).
   - The SHA-256 of the **decompressed** entry bytes is recorded, plus the zip's CRC-32 for the entry.
   - `edisc-verify` must extract and verify entries from the zip offline. The custody package therefore
     carries the zip (or the referenced zip objects).
2. **Reconciliation:** a new status `matched_against_archive`.
   - The report must state that completeness is relative to the provided export, and that the export's
     own completeness against the source is NOT verified.
   - It is never presented as equivalent to a live `matched`; it is not "clean" in the API's `clean` flag.
3. **Real exports:** you will provide a Developer Program sandbox export and a free-plan workspace export.
   Until then, build against the format docs and the dummy (`slack_export` dialect). Both become fixtures
   when they arrive.
4. **Relativity RSMF validator:** run it in CI in addition to the schema check, only if its licence permits.
   Terms found (2026-10-02):
   - **Validator SDK** (`Relativity.RSMFU.Validator.SDK` 2.4.0 on NuGet, .NET Standard 2.0 / .NET
     Framework 4.6.2) is proprietary. "This software may only be used by persons authorized to use …
     Relativity under a valid license agreement with Relativity ODA LLC." It forbids reverse engineering
     and copying.
     - It is usable in our CI only if the organisation holds a Relativity licence (please confirm).
     - It must stay in a private CI image, never redistributed or shipped.
   - **Sample repo** `relativitydev/rsmf-validator-samples` is BSD-3-Clause (kCura LLC, 2016). Its bundled
     Relativity DLLs are under a separate commercial agreement.
   - **Schema** `RSMFManifestSchema/rsmf_schema_2_0_0.json` is in that BSD-3 repo, so we can vendor it
     with the licence notice.
   - Plan: vendor the schema now. Add the containerized validator only after the licence is confirmed.
5. **Renders:** RSMF creation is limited to `matter_manager` and `tenant_admin` (new permission
   `export.create`) and is audited. Reviewers get previews only.
6. **PDF report:** required. It is rendered from the same HTML template with fixed metadata (creation date,
   producer, document id from the report hash inputs) so output is byte-reproducible. It is hashed and
   recorded as a custody event like the HTML and JSON versions.
7. **UI auth:** backend-for-frontend with server-side sessions, httpOnly SameSite cookies, CSRF protection
   and server-side session revocation. No tokens in the browser.
8. **Anchor storm:** fixed first (migration 0017; docs/runs/2026-10-01-audit-burst.md, "After the fix").
## Open decisions (none blocking M14)
- Confirm the Relativity licence, before adding the RSMF validator to CI (M15).
