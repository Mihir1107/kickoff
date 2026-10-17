# Handoff (2026-10-17: the fixes to step 6 (pending_frontend gate, Microsoft consent without wids, token journal) and M17 step 8 (access lost, check-connections) built; next: the report stating how a pause ended, directory snapshots, real-tenant confirmation)

This file alone is enough to continue. Rules are in `CLAUDE.md` (read it first, in full). The M16
design of record is **`docs/adr/0018-collection-report-and-preview.md`** (decisions §1-§18, notes
§19-§30 and §32, **every deviation in §31**); the rest of the plan of record is `docs/ARCHITECTURE.md`,
`docs/adr/`, `docs/plans/phase-2.md` and `docs/BACKLOG.md`. M16 summary:
`docs/runs/2026-10-12-m16-complete.md`. Expert guide: `docs/VERIFYING.md`. Demo: `docs/DEMO.md`.
**M17 plan: `docs/plans/m17.md`** (every decision taken: its §12; design changes in ADR 0016
Amendments 1 and 2; build notes for steps 2, 3 and 3b at the end of ADR 0016). Steps 0, 2, 3 and 3b
4, 5 (SSE), 5b (download cut-off), 6 (install flows) and 8 (access lost, without the directory
snapshots) are built (3b approved; its confirmation on real Entra and Okta tenants runs in parallel,
the user is setting the tenants up; step 6 has its own items to confirm on the real apps); step 7 is
on hold (legal review). **Phase 3 decisions so far: `docs/plans/phase-3.md`.**

## Current state
**Branch and CI:** `main`. Repo: `github.com/Mihir1107/kickoff`.
Migrations at head: **0042** (0041 the token journal fix, 0042 access lost, M17 step 8). Renderer **1.3.2**, dummy connector **0.5.0**, normalizer
**0.3.0**, report renderer **1.5.0**, preview renderer **1.0.0**. CI: run 38716016425 green on
`5f0774e` (pool exhaustion, every job); run 38722400091 green on `4c0d513` (step 3b, every job;
the unit job prints the real-IdP skips).
Run 38577569420 green on `43f41b4` (step 3); run 38536427956's failure on `4a8c05f` is diagnosed
and fixed (below).

## Done: the fixes to step 6, then M17 step 8 (2026-10-17)
**CI:** see "Current state" (the runs on the two commits of this session).

**Found first: CI run 38907553613 on `8172a9a` (the HANDOFF commit) had FAILED** (the previous
session reported step 6 green on `da6efe0`, which it was; the HANDOFF commit after it was not):
- `api-compat`: the two D10 allow-list entries matched nothing once their base already had the
  change, so the check failed as designed for stale entries. Fixed by fix 1 (pending entries are
  standing records).
- integration shard 0: `test_50k_messages_survive_sigkills...` failed the loop guard once (one
  275 ms stall in a spawned worker, 37 % loop CPU, in a non-blocking asyncpg send). DIAGNOSED:
  host starvation, same class as ADR 0015 §24.11; reproduced the shape locally (4 runs under
  8-core contention, all passed, stalls of 286-317 ms at unrelated frames with 12-15 % loop CPU).
  No product change, nothing loosened. ADR 0015 §24.18 has the data and what to check if it recurs.

**Fixes to step 6 (ADR 0016 "Implementation notes: the fixes to M17 step 6"; round
`m17-step6-fixes` 16/16; migration 0041):**
1. **Allow-list `pending_frontend`:** an entry names EITHER the frontend commit that absorbed the
   change OR `status: pending_frontend` + `tracking: <file>.md#<anchor>` (the file must hold
   `<!-- pending-frontend: <anchor> -->`). The two D10 entries are converted (tracking
   `docs/HANDOFF.md#install-routes`). `scripts/production_readiness.py` (`make
   production-readiness`) fails while any exists: CI job `production-readiness` (red until the
   frontend absorbs the install routes; blocks the run only on `v*` tags, `continue-on-error`
   elsewhere) and the report image's production target `report` (its `readiness` stage; the demo
   now builds `runtime`). **So `production-readiness` is EXPECTED red on main until the frontend
   moves to the install routes.**
2. **Microsoft consent without `wids`:** the gate is the consent token (client credentials for the
   `tid`: that tid, our `appid`, application roles); `wids` when present must name an administrator
   (else refused), when absent nothing is skipped; `admin_evidence` in the completed audit.
   To confirm on a real tenant: `appid` in Graph app-only tokens.
3. **Token journal:** entries deleted with their apply (refresh and install), only sealed blobs
   (database CHECK), a received entry undeletable (trigger); an install's entry carries its
   completion facts and the worker's reconciler (`edisc_worker.install_recovery`) finishes the
   install exactly once (tested: two reconcilers racing plus a rerun).

**M17 step 8 (ADR 0016 "Implementation notes: M17 step 8"; round `m17-access-lost` 20/20; migration
0042):** reason classes on `AuthenticationError`; pause deadline from the tenant setting
`reauth_deadline_hours` (24 h..90 d, default 168; `GET /v1/tenant/settings`, `PUT
/v1/tenant/settings/reauth-deadline`), warning time, one alert per job + one warning alert, alerts
resolved when the pause ends; `check-reauth-deadlines` (5 min) closes every unsettled unit
`access_lost` with the class, each with its chain event, then `stop_reason = access_lost` and the
job finalizes `completed_with_gaps`; `POST /v1/jobs/{id}/finish-without-access` (`job.resume`,
reason, recent sign-in, audited); `check-connections` (every 6 hours, `Connector.check_credentials`).
**Found:** a job whose credentials were refused during enumeration has no units and would have
finished `completed`; it now fails (`units_enumerated: false`). **Found (intermittent hang, 1 in 9
runs):** a unit activity holding the REVOKED credentials could fail after a re-authorization and
pause the resumed job again for ever; activities now stamp the credentials they loaded and a
refusal pauses only the credentials in force (deterministic test; 5/5 under contention).
**Not built in step 8:** the directory snapshots (plan §3.3), the SSE `connection` event, the
report stating HOW a pause ended (needs a report renderer bump + goldens + PDF goldens in the
image), live Slack/Teams `check_credentials` (Phase 3 connectors; until then their connections are
counted `unchecked`).

## Done: the fixes before step 6, then M17 step 6 (2026-10-16, second session)
**CI:** run 38894640908 on `3665ca1` (the fixes): every job green but integration shard 2, which
failed `test_stalled_clients` -- DIAGNOSED as a test bug (below), fixed in `c6ef043`. Run
38897021941 on `c6ef043`: green but shard 2, which failed the step-5 replay test -- DIAGNOSED as a
PRODUCT bug (a snapshot read across READ COMMITTED statements, item 7 below), fixed with step 6.
**Run 38905234604 green on `da6efe0` (step 6 + both fixes), every job** (api-compat,
production-install with the D10 check, integration 0-3, report-pdf).

**Fixes before step 6 (commit `3665ca1`; ADR 0016 "Implementation notes: the fixes before step 6";
round `m17-pre6` 8/8):**
1. **Stalled readers:** every write of a content download and of the job stream is timed
   (`EDISC_DOWNLOAD_STALL_SECONDS`, 60 s: TCP's own retransmission backoff gives up on a silent
   peer after about a minute); a stall cuts, audited `download_aborted` reason `stalled`.
2. **Found: the request-context middleware was a `BaseHTTPMiddleware`**, which buffers streamed
   bodies in its own task: a cut never reached the server (a stalled download would never have
   been closed) and `bytes_sent` counted an undelivered write. Now plain ASGI; a unit test refuses
   `BaseHTTPMiddleware`.
3. **Found: a stalled download held a pooled S3 connection** (`EvidenceWriter.open` and package
   object members kept a GetObject body open across writes; 50 stalled clients = the S3 pool).
   Now `worm.read_ranges` (each range read whole, connection returned before the yield). Tested:
   40 stalled natives + 10 render packages stalled inside a >1 MiB member + 10 stalled streams:
   no database connection held across the stall, none in Redis, none lent by S3; another client
   downloads and reads normally.
4. **Permission loss cuts downloads** (`permission_lost`, every content route; `still_permitted`).
5. **CI:** cancel-in-progress only off main; every main commit has its own concurrency group.
6. **CI shard failure diagnosed (test bug):** the database check sampled the pool at one instant
   while the stalled streams' feeds polled; every checkout in the stall was traced (feed polls,
   session checks: short, none across a write). Now: whatever is out when the stall begins must be
   back after three poll cycles. The same work showed the package half of the S3 check had been
   vacuous (members <= 1 MiB are read whole before they are written); packages now stall inside a
   >1 MiB member and the S3 assertion catches that break itself.
7. **CI shard failure diagnosed (product bug in step 5):** the feed's poll read the head, then
   the counts in later READ COMMITTED statements: a unit committing between them produced a
   snapshot named "head A" that showed unit B (sent before B's `units`). The catch-up and the poll
   now run REPEATABLE READ (`tenant_tx(..., repeatable_read=True)`); a deterministic test commits a
   unit between the two reads.

**M17 step 6, source installs (ADR 0016 "Implementation notes: M17 step 6"; round `m17-installs`
16/16):** migration 0040; `POST /v1/clients/{c}/connections/slack/install` and `.../teams/consent`
(a browser session + `connection.manage`; `{authorize_url, connection_id}` + the Lax
`__Host-edisc_install` cookie); `GET /v1/oauth/{provider}/callback` (relay host `oauth.<base>`:
state checked without a read, row looked up, MAC recomputed, 303 to the row's tenant host; tenant
host: every check again + consume + cookie + session row + permission, then Slack's exchange or
Microsoft's OIDC + client credentials, `bind_external`, tokens journaled first). Tests (19) cover
open redirect, replayed / other-tenant / expired / forged states, the cross-customer attacks for
both providers, tokens never in logs, audit payloads or pages. D10: the generic credential routes
exist only in local/test/ci (two allow-list entries). Operator CLI `python -m edisc_api.admin
move-binding` (A2.3). **To confirm on the real apps:** Slack PKCE (`EDISC_SLACK_PKCE`, off), Entra
`wids` in the ID token, the Graph app roles.

## Done: the step-5 fixes, then M17 step 5b (2026-10-16)
**CI:** run 38889003488 green on `4068bca` (5b, which contains the fixes), every job. Run
38887605999 on `45c8aba` (the fixes alone) passed lint, typecheck, unit, api-compat, report-pdf and
production-install; its integration shards were CANCELLED by the 5b push (`cancel-in-progress`), so
the fixes' integration result is the 5b run's.

**Step-5 fixes (commit `45c8aba`; ADR 0016 "Implementation notes: the step-5 fixes"; round
`m17-stream-fixes` 17/17):**
1. **Bounded replay:** `EDISC_SSE_REPLAY_MAX` (10,000 unit events; `LIMIT cap+1` tells "at" from
   "over") and, for reconnects, `EDISC_SSE_REPLAY_MAX_AGE_SECONDS` (1 h, the oldest replayed
   event, database clock); beyond, or a `Last-Event-ID` past the head: ONE `reset` event (`id` =
   head, `reason` `replay_limit` / `replay_age` / `unknown_position`), the snapshot, then live.
   Published (`ResetEvent`, `ResetReason` in `x-sse-events`). Tested at the cap, one over, fresh
   start, age, past the head.
2. **Found:** the 15 s bound could be 30 s for a second stream of one session (the watch cached for
   the whole bound and each stream asked once per bound). Now two halves (`SessionWatch.interval`).
3. **422 bodies as sent:** `ValidationErrorOut` (closed) / `UnprocessableOut` (detail issues or
   text; open to server-state fields: found the export routes' `digest_mismatch` carries the
   hashes). The test client validates EVERY error body against the document;
   `test_error_bodies.py` checks real bodies against the committed file. Allow-list entries may
   list `operations`; the one pre-client entry (34 list operations, frontend `aeee68e` reads no 422
   type) was removed in the 5b commit, as merged entries must be.
4. **Headers** `no-cache, no-store, no-transform` + `X-Accel-Buffering: no`; **real uvicorn**
   (`test_stream_uvicorn.py`): first event < 5 s through `create_app` and behind GZipMiddleware (the
   next write would be the 7.5 s keepalive, so any buffering exceeds it).

**M17 step 5b (ADR 0016 "Implementation notes: M17 step 5b"; round `m17-cutoff` 10/10):**
`edisc_api.downloads.guarded` on all eight content routes (evidence content, render file, native,
render package, job package, report file, report package, preview): session (cookie) or principal
(bearer) re-checked between pieces of at most `EDISC_API_DOWNLOAD_CHUNK_BYTES` (64 KiB) through the
shared `SessionWatch`; a cut writes `download_aborted` (route, ids, exact `bytes_sent`, size, reason,
`read_seq`), anchors it, then raises `DownloadCut` (connection reset short of the exact
`Content-Length` every content route now declares; evidence content, render file and preview did
not). Tests: one cut per route (`tests/integration/api/cut.py`), packages cut are not zips, a bearer
principal deactivated, a steady reader at the production 15 s bound, the client's view through a
real uvicorn (`RemoteProtocolError`), and a unit test that every route anchoring a read is guarded
and has a cut test. Not covered by design: a client that stops reading (nothing leaves until it
reads; the next boundary cuts); permission loss mid-download (A5 names session and principal).

## Done: error codes, then M17 step 5 (2026-10-15, third session)
**CI:** run 38745179430 green on `98044d1` (error codes); run 38766458358 green on `4af4912` (step 5,
every job including `api-compat`).

**Error codes (commit `98044d1`, CI run 38745179430 green; ADR 0016 note "every route declares its
error codes"; round `m17-error-codes` 8/8):** one registry (`edisc_api.error_codes.E`, one status
per code); each route declares its handler's codes in `perm(...)` / `public(...)` /
`authenticated(...)`; `complete()` adds the codes implied by the operation's shape and writes
`x-error-codes` + one response per error status into `docs/api/openapi.json`. The API test client
checks every error answer (`tests/integration/api/error_check.py`): declared for its operation, the
registry's status, a code at all. It found FastAPI's body-parse 400 without a code: framework
refusals now answer `malformed_body` / `not_found` / `method_not_allowed`. The compat rule
`edisc-error-code-removed` is proven on the published document.

**M17 step 5, job streams (ADR 0016 "Implementation notes: M17 step 5"; round `m17-stream` 15/15):**
`GET /v1/jobs/{id}/stream` (`edisc_api.stream`, `stream_events`; round now 16/16): events `job` / `units` / `end`
(reasons `sealed`, `session_revoked`, `session_expired`, `principal_deactivated`,
`permission_lost`, `slow_consumer`, `lifetime`); ids follow the job's custody chain, so a
`Last-Event-ID` reconnect misses and repeats nothing (tested, including a feed kept behind on
purpose); one poll per job per process per second (D4); session/principal and `job.read`
re-checked <= 15 s (D3, tested at 15 s), passive authentication (D6, tested with the session row);
a bounded queue per subscriber, overflow ends with `slow_consumer` (tested with a client that stops
reading); 10 streams per principal; no evidence content (a real collection streamed to its seal,
none of its item strings or custody payload values in the bytes); `x-sse-events` (the compat rule
proven on the document). Found while building: a seal that moves neither status nor chain head was
never seen (intermittent hang; fixed + deterministic test); units sent twice when a feed's event
overlapped a newer catch-up (per-entry `seq` trim); the local KMS loaded the RSA key on the event
loop (now a thread); a client gone during its catch-up left a subscriber (a feed polling for ever)
and could interrupt a transaction (now shielded, tested); the error-code check itself blocked
tests' loops on first use (built once in a thread now; ADR 0015 §24.16); the API's Redis pool failed bursts with unhandled 500s (now fixed, blocking,
503 `service_busy`; round `s24-pool-exhaustion` 8/8). **Measurement**
(docs/runs/2026-10-15-sse-500-streams.md): 500 streams on 50 jobs: 298 statements/s, p99 1.10 s,
every delivery; on 500 jobs: 1,062 statements/s, p99 1.69 s; about 72-75 KB of Python per open
stream. (5b, the download cut-off, built 2026-10-16: above.)

## Done: the groups claim, then M17 step 4 (2026-10-15, second session)
**CI:** run 38734057961 green on `a6fa14c` (groups), run 38736314014 green on `9056361` (step 4,
every job including the new `api-compat`, which printed "nothing to compare" for its first spec).

**Groups claim (commit `a6fa14c`; ADR 0016 note "the groups claim is never read as no groups"; round
`m17-groups` 6/6):** Entra's overage (`_claim_names`/`_claim_sources`, `hasgroups`) refuses the
sign-in (`auth_error=groups_overage`, 403) and bearer tokens (generic 401) whatever the mapping; an
absent or malformed claim refuses them (`groups_missing`) when roles depend on groups (a live role
held by a group with an `external_id`); a present empty list is accepted. Audited
(`authentication_refused`; `login_failed` for sign-ins), one alert per principal and reason while
unacknowledged. Migration 0039. Test tokens (`TenantCtx.token`) now carry `groups: []` by default.
BACKLOG: Graph group fetch on an overage. **Okta to confirm:** a filtered groups claim is omitted
when nothing matches, which would refuse such users in a tenant with group-based roles.
**A loop block diagnosed on the way (ADR 0015 §24.15):** async tests ran `edisc-verify` through a
synchronous `subprocess.run` on their loop (8 files); the helpers now run in a thread. Test bug.

**M17 step 4 (ADR 0016 "Implementation notes: M17 step 4"; round `m17-openapi` 20/20):**
`docs/api/openapi.json` (`make openapi`; operationId = function name, generated from production
settings independent of the environment), the drift check (`make openapi-check`, a unit test, CI job
`api-compat`), `scripts/api_compat.py` (oasdiff 1.33.0 pinned by archive SHA-256 + our rules +
`docs/api/breaking-allowlist.yaml`; `make api-compat BASE=<ref>`). Fixture pairs showed oasdiff 1.33
silent on six §5 classes (non-success status removed, optional response field removed, response enum
value removed, default changed, media type changed, operationId changed): our rules cover them; it
rates a new response enum value ERR, downgraded to a warning (D9). **Inert until routes declare
them:** the `x-error-codes` rule (no route documents its error codes) and the `x-sse-events` rule
(step 5). Step 6 (A7) will remove the generic credential routes from this production document: an
allow-list entry then.

## Done: before step 3b, then M17 step 3b (2026-10-15)
**Before 3b (commit `5f0774e`, CI run 38716016425 green on every job):**
1. CLAUDE.md rule: never loosen a guard, threshold or exemption to make a test pass; a guard firing
   is a bug until diagnosed (the anchor-claims test failed twice on connection churn while the loop
   guard was given an exemption instead).
2. **Pool exhaustion fails loudly** (ADR 0015 §24.14): a checkout beyond a fixed pool waits
   `EDISC_PG_POOL_TIMEOUT_MS` (default 10 s, 1 ms..120 s), then raises (`is_pool_exhausted`). API: 503
   `db_pool_exhausted` + `Retry-After: 1` + an error line `db pool exhausted` (never a 500 or a hang);
   workers: `Transient` (retried with backoff). Tests: `tests/integration/db/test_pool.py` (a checkout
   and a real activity on a held one-connection pool, bounded, retryable, recovers),
   `tests/integration/api/test_pool_exhaustion.py`. Round `s24-pool-exhaustion` 5/5.
3. **BACKLOG hardening (required before production):** the connection budget (API replicas x 20 +
   worker processes x 22 + one direct connection per concurrent evidence content lock, vs
   `max_connections`), and PgBouncer in transaction mode: asyncpg / SQLAlchemy prepared statements,
   the tenant setting, the start-up `server_settings` (not forwarded by PgBouncer: move to `ALTER ROLE
   ... SET`), the content lock's SESSION-level advisory lock (cannot go through a transaction pooler).
   **Verified: every write of `app.tenant_id` is transaction-local** (`tenant_tx`'s
   `set_config(..., true)`, the `create_tenant` definer function's `set_config(..., true)` + restore,
   the db test helper's `true`); no session-level `SET` exists in application code.

**M17 step 3b, `private_key_jwt`** (ADR 0016 "Implementation notes: M17 step 3b"; round
`m17-client-auth` 27/27): migration 0038 (`tenant_idps.login_client_auth`, default
`private_key_jwt`), `edisc_api.client_auth.ClientSigner` (RS256 assertions signed by a KMS RSA 3072
key: `kid` = RFC 7638 thumbprint, `x5t` + `x5t#S256`, `aud` = discovered token endpoint, 5-minute
`exp`, random `jti`; PKCE kept), `KmsSigner` on the local KMS (`get_public_key`, `sign` over a
digest), `EDISC_API_CLIENT_KEYS` (current first; the API refuses to start without one outside
local/test/ci; a dev key in the local KMS otherwise), a deterministic self-signed certificate
(identical bytes on every replica), `GET /.well-known/edisc-client-jwks.json` and `-cert.pem` (base
host only), `none` refused outside local/test/ci (generic 401, audit, alert; re-checked at the
callback), the discovery check (fail closed at the start), `exchange_invalid_client` recorded. The
test IdP now treats the sign-in client as confidential and checks assertions like Entra/Okta; every
sign-in test (the three-browser A1 test included) runs with `private_key_jwt`.
**Real tenants: NOT confirmed yet.** The list of claims and settings to confirm is in ADR 0016 ("To
confirm on real tenants"); `make test-real-idp` runs `tests/real_idp` against a tenant
(`scripts/real_idp.py prepare` makes the key, certificate and JWKS to register; the tests skip
visibly without `EDISC_REAL_IDP_*` and fail when half-configured; CI's unit job shows the skip).

## Done: the CI stall, the cookie proof, M17 step 3 (2026-10-14, second session)
**CI:** run 38571230066 on `33da278` (the pool fix, the cookie test, step 3b in the plan): every
job green (integration shards 0-3, report-pdf, production-install). Local: full integration suite
1,251 passed before that commit.

**M17 step 3, the UI's reads (round `m17-reads`, 17/17; ADR 0016 "Implementation notes: M17 step
3"):** migration 0037 (`ix_collection_jobs_tenant_created`), `GET /v1/me/permissions` (scopes held
directly or through groups, with roles and permissions; keyset on scope), `GET /v1/roles` (from
`ROLE_PERMISSIONS`), `GET /v1/jobs` (tenant-wide, newest first; visibility = `authorize`'s rule as a
join, workspace roles do not see jobs, exactly like the GET; filters status, clean_basis, client,
matter, connection, source, created/finished ranges), `GET /v1/jobs/{id}/custody/events`
(`custody.read`, keyset on seq, <= 500, 1 MiB payload budget measured in SQL), `GET /v1/groups`,
`GET /v1/groups/{id}/members` (`include_removed`), and every list cursor bound to its route, path and
filters (`pagination.bind_cursors`, app-wide; 422 `cursor_mismatch`; old cursors are refused). Lists
build rows in three statements per page (`_jobs_out`). Tests: `test_ui_reads.py` (17), the access
matrix extended (custody events, jobs, groups), the no-echo fuzzer covers the new routes. The plan's
later migrations are renumbered (step 3b 0038, installs 0039, directory 0040).

## Done: M17 decisions, the log sweep, M17 step 2 (2026-10-14)
**Decisions (user, 2026-10-14; ADR 0016 Amendment 2; m17.md §12):** signed state, alert timing and
Slack exports outside the one-workspace rule approved; access-lost deadline 24 h..90 days (default 7
days); moving an external id between tenants = operator CLI only (no API route; a reason; both
tenants' audit streams; both tenants' admins alerted; a test that no route exposes it); D4 approved
+ a 500-concurrent-stream measurement in step 5 (DB queries/s, p99; docs/runs); D6, D7, D9 (frontend
renders unknown enum values safely: below), D14 approved; D8 to BACKLOG, required before production
(SCIM, back-channel logout); D12 default kept pending mentor; D13 confirmed no. New: Slack's 1
request/minute `conversations.history` limit for non-Marketplace apps (tier detection reported as a
job condition, never a silent crawl) in ADR 0016 A2.9 and `docs/plans/phase-3.md`; the internal-app
token exception is **pending legal review**: step 7 not built, no ready-made app manifest shipped.

**Log-assertion sweep (commit `41dc6e3`, round `m17-logs`, 7/7):** every test asserting on log
output now reads the production pipeline and proves its lines arrived (`tests/logcapture.py`: a
capture that fails when empty + `require()` positive controls). Four were blind: the connection
credential test (`caplog`), the canary-as-host check (the client lowercases hosts), the unit 422
checks (their app had no access line, so the capture was empty), the KMS "redacted from logs" test
(never logged). Each blind one was broken once against its old version (passed) and its new version
(caught). The request middleware is now `edisc_api.app.add_request_context`.

**Step 2, browser sessions (round `m17-sessions`, 35/35; three were uncaught at first and their tests were fixed, ADR 0016 step-2 notes):** migration 0036
(`sessions`, `login_attempts`, `tenant_idps.login_client_id`), `edisc_api.sessions`, `edisc_api.oidc`,
`edisc_api.routes.auth` (login, callback with the A1 bounce page, logout, CSRF), own/admin session
routes, deactivation ending sessions in its transaction, the cookie path of the caller dependency
(one credential, fetch metadata, CSRF token + Origin), `require_recent_sign_in` real on every §4
action. The choices made while building are numbered in ADR 0016 "Implementation notes: M17 step 2"
(**read them: two need you**, below). Tests: `test_sessions.py` (74), `test_sessions_browser.py`
(Chromium, Firefox, WebKit over real HTTPS through a CONNECT proxy; CI now installs all three),
`test_session_primitives.py`, `test_session_guards.py`. Settings: `EDISC_API_SESSION_*`,
`EDISC_API_REAUTH_MAX_AGE_SECONDS`, `EDISC_API_LOGIN_*`, `EDISC_API_PUBLIC_SCHEME`; the API refuses
to start outside local/test/ci without `EDISC_API_SESSION_KEYS` (workers never need them).

**Found on the way:** two runs on one up test stack wipe each other's `--basetemp` (the second
`test-integration-only` deleted the first's dev IdP key: `InvalidSignatureError` 401s in an unrelated
render-package test). Diagnosed, not re-run to green: `stack_lock.py run` (an exclusive `flock` for
the whole run) now guards `test-integration-only`; ADR 0015 §24.12.

**DIAGNOSED AND FIXED (2026-10-14, ADR 0015 §24.13):** CI run 38536427956's anchor-claims stall
(466 ms, 527 ms of loop CPU, sampled on a `BEGIN` send) was database pool churn: `create_engine`
had `max_overflow=pool_size`, so every burst opened and closed dozens of connections, each running
asyncpg's Python PBKDF2 SCRAM handshake on the loop. Reproduced (old settings 4/10 fail under 8 busy
cores, the fix 0/10; a scratch Postgres measurement: stall = number of new connections). Fix: fixed
pools warmed at startup (`warm_pool`), churn after warm-up fails tests (`loopblock.report_product`),
and the guard counts an "idle" sample when the loop thread was measured busy. Round `s24-pool` 7/7.

**Resolved (user, 2026-10-14):** `private_key_jwt` is M17 step 3b (plan §1.6, off the BACKLOG); the
`__Host-` session cookie is proven host-only in three browsers
(`test_the_session_cookie_never_reaches_another_subdomain`, ADR 0016 step-2 note 12; round
`m17-host-cookie` 2/2), the `same-site` refusal kept as the second layer.

## Frontend requirements (for `feat/web-ui`; the backend never edits `apps/web`)
- **Unknown enum values (D9, ADR 0016 A2.7):** a response enum value the UI does not know renders
  as a neutral "unknown (value)" presentation: never a crash, never treated as a known value. New
  response enum values are not breaking changes for the API.
- **Sign-in (M17 step 2):** go to `/v1/auth/login?return_to=<path>` (a same-origin path; 400
  `invalid_return_to` otherwise), `reauth=1` when an action answered 401 `reauth_required`. The
  callback answers a bounce page that lands on `return_to`; failures land on `/?auth_error=<code>`
  (`denied`, `expired`, `invalid`, `unknown_user`, `inactive`, `reauth_mismatch`, and since
  2026-10-15 `groups_overage`, `groups_missing`: the IdP's groups could not be read; show "your
  organisation's sign-in is misconfigured, contact your administrator", never a retry loop).
- Every state-changing request sends `X-CSRF-Token` from `GET /v1/auth/csrf` (re-fetch after any
  sign-in: the token is bound to the session) and is same-origin. Logout: `POST /v1/auth/logout`.
  Sessions: `GET /v1/me/sessions` (`current`, `live`), `POST /v1/me/sessions/{id}/revoke`.
- `pending.ts`'s `CsrfOut` matches what shipped.
- **The contract (step 4):** generate types from `docs/api/openapi.json` on `main`; operationIds are
  the route function names (they changed once, from FastAPI's path-based ids, before any generated
  client). A breaking change needs `/v2` or an allow-list entry naming the frontend commit that
  absorbs it; new response enum values may appear at any time (render them as unknown).
- **Job stream (step 5):** `EventSource('/v1/jobs/<id>/stream')` (same-origin cookie). Events
  `job` (snapshot), `units` (settled units with their chain `seq`), `end` (`data.reason`; the
  server closes: reconnect only for `lifetime`, re-authenticate for `session_*`, stop for
  `sealed` / `permission_lost` / `principal_deactivated`, reconnect for `slow_consumer`). The
  browser resends `Last-Event-ID` itself; nothing is missed or repeated. **`reset`** (since
  2026-10-16: too many or too old units to replay): reload the job and `/units` from the list
  endpoints, keep the stream open (the snapshot and live events follow).
- **Downloads (5b):** a download can be cut when the session ends, the permission is lost or the
  client stalls 60 s: the connection closes short of its `Content-Length`; treat it as failed
  (never save a partial file), then re-authenticate.
- <!-- pending-frontend: install-routes -->
  **Installs (step 6), REQUIRED before production** (allow-list `pending_frontend`, tracking
  `docs/HANDOFF.md#install-routes`; production builds are refused until it is absorbed): the live client (`src/api/http.ts`
  `createConnection` / `reauthConnection`) calls routes that no longer exist outside
  local/test/ci. Use `POST /v1/clients/{c}/connections/slack/install` or `.../teams/consent`
  (body `{}` or `{"connection_id"}` to re-authorize; same-origin with `X-CSRF-Token`), then navigate
  the browser to `authorize_url` (only `https:` on `slack.com` / `login.microsoftonline.com`). The
  user comes back to `/clients/<c>/connections/<id>?install=ok` or `?install_error=<code>`
  (`denied`, `expired`, `replayed`, `invalid`, `mismatch`, `already_bound`, `failed`, `refused`).
- **Access lost (step 8):** a job paused for re-authorization shows its deadline (alerts
  `job_paused`, `reauth_deadline_near` carry it); `POST /v1/jobs/{id}/finish-without-access`
  (`{"reason"}`, `job.resume`, a recent sign-in: 401 `reauth_required` means sign in again with
  `reauth=1`; 409 when the job is not paused or already decided) answers the job still paused: its
  units close in the background and it finishes `completed_with_gaps`. Tenant admins:
  `GET /v1/tenant/settings`, `PUT /v1/tenant/settings/reauth-deadline` (`{"hours": 24..2160,
  "reason"}`; 422 outside).
- **422 bodies:** `ValidationErrorOut` / `UnprocessableOut` in the document (regenerate types). 429 `too_many_streams`
  above 10 open streams per user. Fallback: poll `GET /v1/jobs/{id}` and `/units`.
- **Error codes:** every operation's `x-error-codes` in the document; `service_busy` (503) and
  `db_pool_exhausted` (503) mean retry after `Retry-After`.
- **Step 3 reads:** `GET /v1/me/permissions` (a hint for showing controls; the API still decides),
  `GET /v1/roles`, `GET /v1/jobs` (filters `status`, `clean_basis`, `client_id`, `matter_id`,
  `connection_id`, `source`, `created_from/to`, `finished_from/to` with an explicit offset),
  `GET /v1/jobs/{id}/custody/events` (pages may hold fewer than `limit` events: a 1 MiB budget),
  `GET /v1/groups`, `GET /v1/groups/{id}/members`. Cursors: send back `next_cursor` with the SAME
  filters (other filters or another route: 422 `cursor_mismatch`; start the listing again).

## Done: before M17 (2026-10-12; ADR 0018 §32; mutation round `m16-export`, 8/8)
1. **The export in the report.** `job_started.export` (new jobs) records the export's size, entry
   count, declared plan, detected tier, tier confirmation, blind spots, tier warnings and validation
   findings (`pipeline.export_record`, from the `slack_exports` row). `report.json`
   `access.export` states them from the chain (`model.export_section`; UNKNOWN for jobs started
   before; a recorded null plan stays "not declared"); the HTML has an Export section ("What this
   export cannot contain"); `edisc-verify --job-package` recomputes it (a forged tier fails).
   Report renderer 1.5.0: report goldens and PDF goldens (`..._toolchain-e24aecbb3dd4`, recorded in
   the image under emulation, verified again without recording; veraPDF 12/12; image breaks 4/4;
   worker OOM/heartbeat tests pass). Corpus: one export case per tier with a declared plan
   (`export_full` Business+, `export_public_only` free, new `export_grid` Enterprise Grid via the
   dummy writer's new `grid` tier = `org_users.json`; default writer output unchanged, so no dummy
   bump; `export_grid` has its own corpus golden).
2. **Export on the tailored collation.** `tests/integration/corpus/test_collation.py` runs
   `export_full` through the API on the ICU database and the default one (`api.conftest.make_api`
   builds the API over any database). The export carries orphan day-file folders
   (`ExportOptions.orphan_folders`) named so the tailored rules reorder the finding's sample; the
   stated export section must be identical on both. The break "export entries' folder order left to
   the database" (migration 0035) is caught only by this test.
3. **BACKLOG:** the standalone `edisc-verify` package that installs without the rest of the codebase
   is now marked required before production, with what it needs (own distribution, import boundary,
   empty-venv CI job, reproducible build, format versions).

## Done: M17 decisions, ADR 0016 Amendment 1, M17 step 0 (2026-10-12; mutation round `m17-d11`, 17/17)
- CI for `0a9277d` (the M17 plan): run 38251573775, every job green.
- CI for `e593d05` (step 0) + `349459d` (amendment): run 38262381669, every job green
  (production-install and report-pdf included).
- **Decisions** (user, 2026-10-12): D1 (one relay host; signed single-use state bound to tenant,
  user, nonce; PKCE; never a query parameter), D2 extended to Slack (external ids only from the
  signed ID token / authenticated token response; ONE external tenant or workspace per tenant of
  ours, a move is an audited operator action: this also answers D13 as "no"), D3, D5 (+ alerts at
  pause and before the deadline; deadline a tenant setting, default 7 days), D10 (production builds
  do not register the generic credential routes; checked in `production-install`), D11 (a class).
  All written into ADR 0016 Amendment 1 (A1-A8) with the tests each needs; ADR 0012's pause row
  points at A6. **Open: D4, D6, D7, D8, D9, D12, D14** (D13 to be confirmed as decided by D2).
- **Step 0 (A8, built):** `edisc_api.safe_errors` (422 body: type, sanitised loc (client-chosen keys
  `*`), schema-only messages, never input/ctx/url; outermost middleware: fixed 500 + a log line of
  exception types and frames only, re-raise after the first byte; log paths keep only static route
  segments; uvicorn's access log off and its error log through ours, the API writes its own access
  line); `hide_parameters=True` on every engine; worker texts in answers through the redactor
  (`RecordedText`, `RecordedDetail`); refusals that quoted the request fixed (unknown source,
  unknown role, render/preview time zone, preview refusals (audit keeps the detail), export size).
  Tests: `tests/integration/api/test_no_echo.py` (OpenAPI-driven canary in every route and field,
  wrong shapes, keys, bodies, headers, bearer, host; responses AND the production logging output via
  `tests/logcapture.py`), `tests/unit/api/test_safe_errors.py`,
  `tests/integration/db/test_statement_errors.py`, a canary through the real uvicorn in the
  production-install test, canaries in the preview refusal test. Found on the way: the existing
  "no credential in logs" check used `caplog`, which never sees structlog lines in tests (structlog
  is unconfigured there and prints to stdout); `tests/logcapture.py` installs the real setup.
- Not done here by instruction: the M17 build itself.

## Found while planning M17 (step 0 fixed the first; the rest is in ADR 0016 Amendment 1)
- **The API reflects request values in 422 bodies, credentials included** (FastAPI's default
  `input` field): a token sent in a misplaced field or with the wrong type comes back in the response.
  Reproduced with the connections route's model shape. Plan step 0 (`docs/plans/m17.md` §0) fixes it
  globally with canary tests; decision D11 asks to do it first.
- ADR 0016 needs an amendment before M17 code (plan §11): Strict cookies are not sent after a
  cross-site redirect (callbacks need a same-site bounce page; installs need a Lax handle cookie); a
  single OAuth relay host; **Teams admin consent must bind the signed ID token's `tid`, never the
  callback's `tenant` parameter** (otherwise one customer can bind another customer's consented Entra
  tenant); revocation cuts in-flight downloads; revoked tokens end as `access_lost` units.

## Next
1. **The report states how a pause ended** (ADR 0016 step-8 note 7): fold `job_pause_ended`
   (`ended_how`, actor, reason, deadline) into the report's pauses section; a report renderer bump
   (1.6.0) with new JSON/HTML goldens (`EDISC_RECORD_REPORT=1`) and PDF goldens recorded in the
   report image (`EDISC_RECORD_REPORT_PDF=1`, by hand), the verifier's recomputation if it states
   them; VERIFYING.md.
2. **Directory snapshots** (plan §3.3, the rest of step 8 as planned; migration 0043 now).
3. **Confirm step 3b and step 6 on real tenants** (in parallel: the user is setting up the Entra
   ID and Okta developer tenants; `make test-real-idp`; record every answer in ADR 0016 "To confirm
   on real tenants"; also: Slack PKCE, Graph app roles, the `appid` claim of an app-only Graph
   token, `wids` no longer required).
4. **The frontend must absorb the install routes** (pending_frontend `install-routes`): until it
   does, CI's `production-readiness` job is red and the production report image cannot be built.
5. Step 7 waits for legal review. Phase 3 live connectors implement `check_credentials` (Slack
   `auth.test`, Microsoft client credentials): until then their connections are `unchecked`.

## Done: the fixes before step 7 (2026-10-12; ADR 0018 §30; mutation round `m16-pre7`, 15/15)
1. **Collation.** Every ORDER BY reaching output bytes reviewed; migration 0035: the remaining
   ordered text columns COLLATE "C" (items keys, storage keys, scope external ids, export
   conversation/folder columns, and the same NAMES in every table: one rule the static test checks
   without alias resolution); query-level COLLATE for the gap-reason union and the derivation
   tie-break. Columns, not the database default (fixed at CREATE DATABASE; managed Postgres
   defaults to en_US). Tests: `tests/integration/db/test_collation.py` (static: every ORDER BY term
   vs a database migrated from scratch IN the test, so a migration break is visible) and
   `tests/integration/corpus/test_collation.py` (the `custodians` and `slack_three_epochs` cases on a
   tailored ICU database (`&C < G < D`, digits reversed; a precondition test proves it reorders) and
   on the default one: RSMF = golden, units.jsonl + derived sections byte-identical, recompute OK,
   package orders code-point). Found on the way: `job_started` lists scopes in the CALLER's order
   (unit events index them by creation order), so `collection_scopes.external_id` order never
   reached the chain (it orders the jobs API and planning); the first mutation of it was therefore
   not catchable by the corpus run and is caught by the static test. Not covered dynamically: export
   ingestion on the tailored database (the API fixtures are bound to the main database).
2. **Verdict offline** (`recompute.chain_verdict`): clean by the chain = one start, one final event,
   `job_finished` `completed`, no stop, every conversation-day unit reconciled and not
   gap/surplus/access_lost/unverifiable/failed (the pipeline's own status rule). A report stating
   clean (`job.clean` OR the banner's clean line) for a chain that is not FAILS; the other way passes
   and prints the report's stated reasons. Unit + end-to-end tests (`test_reports.py`: a report made
   by code calling every job clean, of a job with gaps, fails ONLY on the verdict; a clean job with a
   retention gap passes and prints it).
3. **Normalizer versions** in `job_started` and each `items_collected` batch (new jobs); the loader
   states the chain's set; the verifier recomputes it (section `versions`). No report renderer bump
   (same inputs, same bytes).
4. BACKLOG: hostile-id conversations collected under the stand-in key; a standalone verifier
   distribution.
5. **A local failure diagnosed, not re-run to green:** the full local suite (1,123 passed) failed the
   50k soak at teardown with two simultaneous 288/292 ms loop blocks in two workers at random library
   frames. Reproduced: not unloaded (worst 55 ms), not in 3 runs under 8-core contention (worst
   246 ms). Cause: host starvation (macOS reclaiming disk + Spotlight + Docker VM, load average 72).
   Recorded in ADR 0015 §24.11 with the candidate mitigation should it recur on CI.

## Done: M16 step 7, docs (2026-10-12)
- `docs/VERIFYING.md` (for a third-party expert): obtaining and running `edisc-verify` (install
  command tested in a scratch venv), what every check proves, what the report recomputation covers,
  what is checked by digest only and why, how to read each kind of FAILED line, checking anchors
  independently, limits.
- Demo (`scripts/demo.sh`, `scripts/demo.py`, `docs/DEMO.md`): collection with a SIGKILL, render
  through the API, render package, report (JSON, HTML, PDF in a report worker CONTAINER of the
  freshly built report image; the API creates reports for its measured toolchain), preview, job
  package, `edisc-verify` on all three (report recomputed from the job chain), two tampers that fail.
  Three runs from clean: 62-70 s with the image cached; toolchain measured in the demo image =
  the admitted `e24aecbb3dd4`. Found while making it run: macOS bash 3.2 + `set -u` rejects an empty
  array (`${net[@]+"${net[@]}"}`); the job and render package routes REFERENCE archives/outputs by
  default, so the demo asks for `archives=embed` / `outputs=embed`.
- ADR 0018: status implemented, §30 (fixes), §31 (every deviation in one table). ADR 0017
  amendment: runtime identities per output kind. CLAUDE.md M16 section consolidated.
- `docs/runs/2026-10-12-m16-complete.md`: what was built, test and mutation counts, measurements,
  open items.

## Open items after M16
Details in the M16 summary and ADR 0018 §31: mentor questions (paper default, name visibility);
before production: report image in a registry + `deploy/render-images.json` + report worker
deployment files, ICC check against color.org, the standalone verifier; not built: per-scope unit
counts, 1M-unit measurement (cloud VM), report reproductions, hostile ids collected under stand-in
keys.

## Done: M16 step 6, the preview (2026-10-12; ADR 0018 §29; mutation round `m16-preview`, 11/11)
`RenderLoader.day_slice` (day-bounded: items by their own time on the 0034 partial expression index,
links by primary key, roots by exact id; 37 statements for one day of a 2-day or a 12-day
conversation), the pure `edisc_renderers.preview` (pages of 1,000 events with context, every message
on one page, the report's sanitiser rules, links only to pinned evidence and same-page fragments,
banners for incomplete/empty days, archive caveat), `edisc_worker.preview.build_preview`, the route
`GET /v1/jobs/{id}/preview` (`evidence.read`; audit + forced anchor before the first byte, CSP of
§10, refusals audited, integrity alert), the Playwright no-remote-fetch test (Playwright 1.63.0, CI
installs Chromium), the D14 measurement (docs/runs/2026-10-12-preview-audit-burst.md: +24 ms per
single view, ~20 % at 50 concurrent; kept, no group commit). `scripts/measure_audit_burst.py` fixed
(it patched the unforced `anchor`; content routes call `anchor_now`) and gained `--target preview`.

## Done: the fixes before step 6 (2026-10-11; ADR 0018 §28; mutation round `m16-pre6`, 24/24)
1. **Hostile export values never drop data.** Names (channel/user/file/workspace) are text: stored
   as replacement + exact code units (`export_conversations.exact`), never refused. Structural values
   are refused as KEYS only: a conversation whose id, team, or (when the winning users.json team holds
   one) workspace holds a lone surrogate is listed under a stand-in id
   (`substitute_key`: `lone-surrogate:<hex units>`) and `refused`; each of its day files is a GAP unit
   with the reason (`UncollectableUnitError`, `work_units.gap_reasons`, collected 0); a hostile `ts`
   is the one element not collected (`ts_lone_surrogate`), a hostile `thread_ts` leaves every message
   collected but the unit a gap (`thread_ts_lone_surrogate`; `RawBatch.gaps`). Corpus cases
   `export_hostile_channel_name` (every message collected and rendered, the name exact in RSMF) and
   `export_hostile_channel_id` (no message collected, every day file a gap with the reason, the rest
   of the export normal); `message_keys` and hostile team ids in their own tests. Before: hostile
   ids/names made the record "invalid" and its messages vanished with NO unit; a hostile team fell
   back to the export workspace; a hostile `ts` failed the unit; a hostile `thread_ts` crashed
   planning. Found on the way: `message_page_subjects` ignored `select`.
2. **Every unit fact in the chain** (new jobs: `job_started.unit_event_format` 2): unit events carry
   kind, conversation, scopes, access-lost reason, gap reasons; `units_unsettled` at the job's end;
   the format is decided per job (`facts_for_format`), older jobs print UNKNOWN. `edisc-verify
   --job-package` rebuilds `units.jsonl` BYTE FOR BYTE plus counts, exceptions, conversations
   (`recompute.recompute`). Still not recomputable (and why): ADR 0018 §28.2. Found: the file order
   depended on the database locale (glibc `en_US` vs code points): migration 0033 makes the order
   columns COLLATE "C".
3. **Job package verifier**: strict (unlisted files fail; `--tolerate-os-metadata` directories only),
   zips read in place (audit-hook test: no write, no temp dir), embedded archives read by random access
   into their STORED entry. BACKLOG item removed (the zip copy was recorded in ADR §27.1, not the
   BACKLOG). Report renderer 1.4.0: report goldens and PDF goldens (`..._toolchain-e24aecbb3dd4`,
   recorded in the image under emulation, veraPDF 12/12) recorded.

## Done: the fixes before step 5, then M16 step 5 (2026-10-11; ADR 0018 §23-§27)
1. **Lone surrogates (§23).** Traced through every stage with the corpus cases `lone_surrogates` and
   `export_lone_surrogates` (dummy 0.5.0 `spec.lone_surrogates`). Before: one escaped `\ud83d` in a
   message failed the WHOLE job (`CanonicalizationError` in the normalizer, JOB_INTEGRITY); once past
   that, the RSMF manifest and `report.json` refused it too. Now: source hashes use
   `canonical_source_json` (lossless escapes, Node-identical, = RFC 8785 for valid input); every stored
   or published record is valid (`edisc_core.lone_surrogates`: replacement text + `lone_surrogates`
   JSON Pointer -> exact UTF-16 code units; error texts via `recorded_text`, `error_utf16` in the
   event); export ingestion refuses such ids/names/`thread_ts`/`team_id` (counted); identities with
   one fail the unit; RSMF 1.3.2 writes U+FFFD + `edisc.lone_surrogates`; the report (1.3.0) counts
   them and the HTML/PDF show the marker. Job package verified offline on the corpus job.
2. **Glyph set in the identity (§24).** It was NOT: the identity named fonts only via the toolchain
   id (`none` outside the image), the HTML golden key not at all. Migration 0032 `reports.glyph_set`
   (identity, unique index, immutable), queue `...u<unicode>.g<glyphs12>`, golden key
   `..._glyphs-<g12>`; tested with a coverage change.
3. **PDF-from-stored-HTML tamper test (§25)** and the `image` mutation kind run inside the report image
   by `make test-report-pdf` (4 image-only breaks, all caught in the local image run).
4. **Production install (§26):** CI job `production-install` / `make test-production-install`: API +
   every worker type from `uv sync --frozen --no-dev --package <app> --no-editable`. Found on the way:
   uv reuses a cached build of a LOCAL package until its pyproject changes, so the venvs pass
   `--refresh-package` for every workspace member (else the test checks stale code).
5. BACKLOG (needs cloud): image registry push + `deploy/render-images.json`, report worker deployment
   files.
Step 5 (§27): `GET /v1/jobs/{id}/package` (job custody package `/3`, deterministic, one generator with
the directory export; off the BACKLOG); reports API (list all generations with who/why, get, files,
file content, package; `report.read` every role, `report.create` admin + manager); report package
`edisc-report-package/1`; `edisc-verify` for report packages and zipped job packages;
`--job-package` recomputes the chain-derived content (`edisc_renderers.report.recompute`). Fixed:
`/v1/evidence/{id}/content` served report files. Mutation rounds `m16-pre5` (28) and `m16-api` (17),
all caught. Also found: a latent sanitiser-fuzz expectation bug (CRLF: each CR gets its marker, so
the page never holds a CRLF pair); the mutation runner treated "no test ran" as caught (now a
problem).

## Done: six fixes before step 3, then M16 step 3 (2026-10-10; ADR 0018 §21, §22; ADR 0015 §24.10)
Fixes (`42951ba`): markers are ELEMENTS (`<span class="cp">`), so literal "[U+202E]" text differs
from a replaced U+202E; HTML-invalid code points (NUL, C0 but tab/LF/CR, DEL, C1, noncharacters, lone
surrogates) are replaced; pages parse under html5lib STRICT; `conversations.jsonl` above 1,000
conversations (tested at 1,000 / 1,001); report goldens `tests/golden/report/` (`EDISC_RECORD_REPORT`);
CI shards: "1067 vs 1068" was the macOS-only ditto test skipped on Linux, and a real defect was found
and fixed (shards were cut before `-m` deselection, so jobs ran a split the verify never checked;
`--verify` now collects per shard like the jobs); BACKLOG SCRAM item (pool minimum, warm-up) open with
the loop-guard exemption. Round `m16-pre3`: 14 breaks.

Step 3 (PDF): report image `deploy/report-image/Dockerfile` (targets `report`, `report-test`),
toolchain id `edisc_worker.versions` (measured with the child's interpreter; `none` outside the image,
local/test/ci only), the PDF child `edisc_worker.report_pdf` (RLIMIT_AS, oom_score_adj, minimal env,
killed on timeout/cancel, `ReportPdfRenderError` retryable), `report.pdf` rendered from the stored HTML
read back by VersionId, glyph coverage in the HTML (`report/fonts/coverage.txt`), print stylesheet with
the banner and page identity on every page, ICC + OFL licences vendored, PDF goldens for toolchain
`e24aecbb3dd4` (6 cases x Letter/A4; recorded under emulation, verified natively by CI), veraPDF 12/12
PASS, the OOM crash point with real worker containers (RLIMIT_AS and cgroup kill paths) and the
heartbeat-hold test. Found on the way: the worker could not start from a production install
(`types_aiobotocore_s3` was dev-only; now a runtime dependency). Round `m16-pdf`: 14 breaks.
Not done (ADR 0018 §22.8): image registry push and `deploy/render-images.json`; a tamper test for
PDF-from-stored-HTML; production deployment files for report workers.

## Done: the three fixes before M16, then M16 step 2 (2026-10-08)
1. **Loop guard blind spot (fix 1, `ef453ee`; follow-up `4d9d52d`).** `edisc_core.loopblock` is the
   deterministic counterpart of `OFF_LOOP`: on a loop thread it fails a test for a blocking socket
   call, `time.sleep(>0)`, a sync DB-driver call (a blocking socket), or a sync file read/write
   >= 64 KiB (ADR 0015 §24.8). Offender fixed: the directory/package export's streaming disk writes
   (now `asyncio.to_thread`). `4d9d52d`: a latent `zip_stream` loop-block the CI exposed (§24.9, now
   yields per chunk).
2. **Test stack lock (fix 2, `3bd3c1f`; follow-up `e7fc73e`).** `scripts/stack_lock.py` + the Makefile:
   a second `test-env-up`/`test-integration` fails fast and leaves the owner's stack intact;
   `test-integration-only` requires an up stack. The §24.6 flaky REPRODUCED under CPU contention and
   is DIAGNOSED + FIXED: `rsmf.eml.aenvelope` base64-encoded a whole chunk in one burst; it now bounds
   the work per loop turn. `e7fc73e`: the loop guard counts a driver's wire protocol in a transport
   read callback as "library" (the SCRAM burst, §24.7), not unattributed.
3. **CI sharding (fix 3, `1574e16`).** The integration job is a 4-way matrix balanced by measured
   duration (`scripts/ci_shard.py`, `tests/integration/durations.json`); `tests/integration/conftest.py`
   deselects the tests not in this shard, and the `unit` job proves the shards are a disjoint cover
   (`ci_shard.py --verify`). Each shard ~10-12 min wall.

**M16 step 2 (HTML):** `edisc_renderers.report.html` renders `report.json` to `report.html` (§3.2, §4):
pure (purity test extended), no template engine, the only text path is `escape`; the sanitiser replaces
bidi controls by `[U+XXXX]`, keeps other Cf/Cc/Zl/Zp + marker, escapes and `<bdi>`-wraps; banner clean
vs not-clean; severity-ordered capped lists naming the remainder by file + SHA-256; every enum value a
row; UNKNOWN where nothing is recorded; one constant `<style>` whose SHA-256 is in the page CSP;
byte-identical per renderer version. Wired into the loader and storage (stored + sealed like the other
files). Tests: `tests/unit/renderers/test_report_html.py` (structural via stdlib `html.parser`, every
hard-string class from S1, hypothesis sanitiser fuzz, determinism) and the integration oracle in
`tests/integration/report/test_report_model.py`. Mutation round `m16-html` (8 breaks, all caught).
**Not done in step 2 (by plan):** `conversations.jsonl` (above-cap only); report goldens via
`EDISC_RECORD_REPORT` (step 1 used oracle + determinism; byte-stability is covered the same way here).

## Done: stage 0 before M16 (2026-10-06/07, ADR 0015 §24; commits fcd3f82, d704ae0)
- `render_store._render_slice`'s thread is pure compute and writes nothing; enforced by an audit-hook
  test in a subprocess (no write-mode open, filesystem change, socket or process from that thread).
- Sweep of every activity (reading + the whole suite under the guard at 50 ms, report mode). Moved off
  the loop: dummy connector page building and plan, page parsing / normalizer / absence detection /
  file refs, item persistence (keys, ids, rows, derivation hashes), batch keys, `verify_chain` page
  folding, render-loader verification / index / messages, the render reconciler, FastAPI's lazy route
  building (`warm_routes` in a thread at startup). Per-chunk yields: `jsonstream`, the RSMF envelope,
  `zipwriter`, `archive.open_entry` (the offline verifier's sync drivers step over a bare cooperative
  turn). Table in §24. To BACKLOG: log redaction recompiles one regex per new secret; asyncpg's SCRAM
  handshake of new connections runs on the loop (pre-warm pools); the render loader's `_index` decodes
  a whole conversation's links in one query.
- Loop guard (`edisc_core.loopguard`, `tests/conftest.py`): a test fails if its loop or a spawned
  worker's was blocked > 250 ms (`EDISC_TEST_LOOP_BLOCK_MS`; why 250: §24.3), with the blocked stack.
  Not counted: garbage collection, a loop in its selector or a non-blocking socket call, asyncpg SCRAM
  handshakes, and stalls where the loop's thread used < 25% CPU (the blind spot fix 1 closes).
  `OFF_LOOP` fails a test whenever a thread-only function runs on a loop thread (found a call timing
  missed). Mutation round `s24-loop`: 37 breaks, all caught.
- CI uploads `integration-failure-<run>-<attempt>` (per failed test: worker logs, Temporal histories,
  custody streams; `tests/integration/artifacts.py`). Proven on the deleted branch `ci/artifact-proof`.
  The integration job limit is 45 min (the suite takes 27-29 min on hosted runners; 30 cut a passing
  run off during teardown).
- ADR 0018 amendment 7: the report PDF renders in a child process with its own memory limit; the OOM
  crash point tests exactly that (design only, built in step 3).
- **Undiagnosed (ADR 0015 §24.6):** one run on the reused test stack had three failures (the three
  tests named in fix 2) and then stalled; its output was lost (killed at the 60-minute limit, `-q`).
  They passed in every later run. Likeliest cause: my own work overlapping that run (a migration file
  in the tree for a minute, heavy commands alongside); NOT proven. Fix 2 addresses it.

## Done: M16 steps 1 and 4 (2026-10-06/07, ADR 0018 §19, §20; commits 6639325, b18050a)
- Step 1: pure model `edisc_renderers.report.model`, loader `edisc_worker.report_loader`, access facts
  in `job_started` (migration 0030 `connections.blind_spots` + the units order index), oracle tests
  for every model case of §15, `scripts/measure_report.py` (memory flat 10k → 100k units, 6.1 → 5.6
  MiB; linear time with the index: docs/runs/2026-10-06-report-scale.md). Round `m16-model`: 21 breaks.
- Step 4 (built BEFORE steps 2 and 3, on the user's order to stop after the custody stream): migration
  0031 (`reports`, `report_files`, `report_id` on custody events and evidence, `render_episodes`
  renamed `production_episodes`), `edisc_worker.reports` (snapshot → `report_started` → files stored
  as locked evidence → `report_generated` → seal), `ReportWorkflow`, `--reports` workers,
  `ensure-job-reports` (a report per sealed job once; `report_missing`, `unroutable`, `sealing_stuck`
  episodes, one alert each). Crash matrix at every boundary, real SIGKILLs at nine barriers, racing
  executors and snapshots. Round `m16-stream`: 13 breaks.
- Known choices: the PDF toolchain id is `none` until step 3 (then a new identity and queue);
  `ensure-job-reports` never retries a job whose report failed or was refused (its `report_missing`
  alert asks a person to regenerate by hand, step 5's API); not built yet by plan: the OOM crash point
  (step 3), API / report package / `edisc-verify` recomputation (step 5).

What M16 is, in one paragraph: every sealed job gets a **collection report** (its own custody stream:
snapshot → `report_started` → files → `report_generated` → seal) made of `report.json` + full JSONL
lists (`units`, `observations`, `renders`, `conversations`) + `report.html` + `report.pdf` (PDF/A-2u,
with compressed streams, rendered by WeasyPrint from the STORED HTML, byte-reproducible inside a
pinned linux/amd64 image), and
reviewers get an on-demand **HTML preview** of conversation-days (not stored, audited and anchored
before the first byte, CSP `default-src 'none'`). Facts come from the verified job chain first; the
database is a cross-check whose disagreements are reported as divergences, never silently resolved.

The decisions you must not re-open (2026-10-06, ADR 0018): WeasyPrint gated by S1 (done, passed);
ADR 0017 generalised to runtime identities per output kind with a report queue
`reports.r<renderer>.p<toolchain12>.u<unicode>`; amd64 only, PDF goldens authoritative only in CI on
amd64 (locally: emulation in the image, or a skip with a visible reason, never silent); paper size in
the report identity (default pending mentor, `letter` until then); PDF dates = job `sealed_at`, the
generation time lives only in the custody stream; vendored sRGB2014 ICC with its licence recorded
(verification against color.org is a BACKLOG item required before production); COMPRESSED streams
(zlib pinned by the Debian snapshot and in the toolchain id; amendment 1); no Japanese font (Han
unification is a documented limitation: Japanese renders with SC glyph forms); report workers: PDF
concurrency 1 per worker, 3 GiB container limit, CPU 1, OOM kill in the crash matrix; image admission
requires the PDF goldens to match byte for byte inside the image; installers not in the toolchain id;
capped HTML/PDF lists (1,000, worst status first then a stable key, exact totals, remainder named by
file + SHA-256); automatic report per sealed job + `report_missing` episode after
`EDISC_REPORT_MISSING_SECONDS` with one alert; manual regeneration records who and why and never
replaces earlier reports; blind spots, plan tier, granted scopes and `unit_day_zone` go into
`job_started` for NEW jobs, older jobs print UNKNOWN (no audit-event lookup); integrity problems
still produce a report that leads with them; `report.read` for every role including collector,
`report.create` for tenant_admin + matter_manager, preview under `evidence.read`; UTC only, each unit
showing its day label and zone as recorded (no tzdata); `edisc-verify --job-package` recomputes the
chain-derived report content; Noto font set (no JP) subset on embed with OFL licences in the repo; one forced
anchor per preview page view (fallback if too costly: group commit, never skipping the anchor).

Steps (details in ADR 0018 §16 and the sections it points to):
1. **Report model, pure** (`packages/renderers/src/edisc_renderers/report/model.py`, DB-free, the
   verifier imports it): records in, `report.json` + JSONL out. The `job_started` payload additions
   (§7.2) in `edisc_worker.pipeline` (custody payload addition; verifier treats them as optional).
   Worker loader (chain pass + DB pass with additive digests and 4,096 buckets, §12). Oracle:
   `tests/integration/report/oracle.py` from `Dataset` + injected conditions (§15 case list).
2. **HTML**: pure builder (no template engine), escaping + `<bdi>` + the marker rules (§3.2: bidi
   controls REPLACED by `[U+XXXX]`, other Cf/Cc/Zl/Zp kept + marker), banners on every page (§4),
   severity-ordered caps; goldens (`EDISC_RECORD_REPORT=1`).
3. **PDF**: turn `spikes/m16-pdf/` into the report worker image (fonts via `fonts.lock` +
   `fetch_fonts.py`, `fonts.conf`, Debian snapshot apt, ICC vendored with `SOURCE.md` and the licence
   text from `spikes/m16-pdf/ICC-LICENSE.txt`, OFL licences under
   `edisc_renderers/report/fonts/LICENSES/`); the toolchain id in `edisc_worker.versions` (prototype:
   `spike.py toolchain`, installers excluded); compressed PDF/A-2u (spike variant `pdfa2u-z`); the
   glyph-coverage pass (prototype: `spike.py visible()`); worker limits (ADR 0018 §6: PDF concurrency
   1, 3 GiB, CPU 1, `EDISC_REPORT_MAX_HTML_BYTES`); a CI job on amd64 inside the image with veraPDF
   (`verapdf/cli` 1.30.2, digest in `run.sh`), the leak test (prototype: `spike.py leak`), PDF goldens
   and the admission check (PDF goldens byte for byte inside the image).
4. **Migration 0030** (`reports`, `report_files`, `evidence_objects.report_id`, `production_episodes`
   generalising `render_episodes`, index `work_units (job_id, conversation_id, day, unit_key)`,
   permissions), `ReportWorkflow` + report custody stream + `ensure-job-reports` schedule +
   `report_missing` episodes; crash matrix with real SIGKILLs (`EDISC_TEST_REPORT_BARRIER`) and an
   OOM kill during PDF rendering (a worker container with a low memory limit; S1 showed 512 MiB is
   killed with exit 137 and leaves no file, 3 GiB completes).
5. **API** (`/v1/jobs/{id}/reports` POST/GET, `/v1/reports/{id}`, files content, package) with
   `first_byte.py` tests; `edisc-report-package/1` and `edisc-verify` recomputation.
6. **Preview**: `RenderLoader.day_slice(...)` (day-bounded; `_index` loads a whole conversation
   today), pure renderer, route with audit + `anchor_now` before the first byte, the CSP of §10, the
   no-remote-fetch test, audit-burst measurement (D14).
7. **Docs**: CLAUDE.md section for M16, this file, ADR 0017 amendment (runtime identities), BACKLOG
   (report reproductions).

Where the data is (verified 2026-10-05/06):
- Chain events the report reads: `job_started` (actor, connector + version, connection, scopes),
  `unit_reconciled` (unit key = `<conversation>/<YYYY-MM-DD>` UTC, expected, collected, recon status,
  file gaps, no-longer-observed, archive basis + day anomalies), `unit_failed` (unit key, error type,
  error text ≤ 2,000 chars), `job_paused` (reason, connection), `job_resumed` (actor = who
  re-authorized), `job_finished` / `job_cancelled` (status, unit summary, `paused_ms`, stop reason),
  `items_collected` batches. Writers: `workers/collection/src/edisc_worker/pipeline.py`.
- Tables: `collection_jobs`, `work_units`, `work_unit_scopes`, `collection_scopes`, `job_pauses`,
  `alerts`, `retention_gaps`, `items` (event kinds in `EventKind`) via `job_items`, `item_derivations`,
  `renders`, `render_files`, `render_natives`, `connections` (`plan_tier`, `granted_scopes`).
- Blind spots today: only in `audit.connection_created` / reauthorize payloads
  (`apps/api/src/edisc_api/routes/connections.py`) and `slack_exports.findings` — the report does NOT
  read them for old jobs (D7); new jobs record them in `job_started`.
- `verify_chain` (`edisc_custody.log`) returns `VerificationReport` (events, batches, items, files,
  anchors, head, errors). `JobStatus.is_clean`, `ARCHIVE_CAVEAT` in `edisc_core.schemas`;
  `clean_basis` / `caveat` in `apps/api/src/edisc_api/routes/jobs.py`.
- Permissions: `apps/api/src/edisc_api/authz.py` (`Permission`, `ROLE_PERMISSIONS`).

## Spike S1 (done 2026-10-06; results in ADR 0018 §5.10, amendments and re-check in §18)
- `spikes/m16-pdf/`: `Dockerfile` (python 3.12.13-slim-bookworm by digest, Debian snapshot
  `20261001T000000Z`, WeasyPrint 70.0, pypdf 6.19.0), `fonts.lock` (URL + SHA-256 per font),
  `fetch_fonts.py`, `fonts.conf`, `ICC-LICENSE.txt`, `spike.py` (`html`, `render` with variants
  `plain`, `pdfa2u`, `compressed`, `pdfa2u-z` (production form), `runs`, `toolchain`, `inspect`,
  `leak`), `run.sh OUTDIR [HOST_FONT_DIR]` (all checks + veraPDF on one host).
- Local run: `docker buildx build --platform linux/amd64 --load -t edisc-pdf-spike:s1 spikes/m16-pdf`
  then `bash spikes/m16-pdf/run.sh <outdir> <a copy of host fonts>` (Docker Desktop cannot mount
  `/System/Library/Fonts`; copy it into a shared dir first). About 30-60 s per 234-page render under
  Rosetta, about 1.2 GB RSS per render.
- Cross-host: `gh workflow run spike-m16-pdf --ref main` (manual only). Job `host-a` builds, runs and
  saves the image as an artifact; `host-b` (another runner) LOADS that image and runs again; compare
  the two `pdf-sha256.txt`. The S1 runs were 37366348776 and 37369595037 (artifacts expire after 7
  days; the results are in ADR 0018 §5.10). The amended image (no JP, no pip in the id) was re-checked
  locally only (§18); re-run the workflow once if you want the four-host check on it.

## Open decisions / waiting on the user
1. **Mentor (ADR 0018 §17, not blocking):** (a) default paper size (Letter likely for the US market;
   paper is in the report identity, the default is one constant); (b) may reviewers and client admins
   see conversation and custodian names in the report and preview (if not: a redaction mode in the
   report identity).
2. **RSMF consumer limits (ADR 0015 §22.4):** Relativity documents that `rsmf.zip` "must ... use
   DEFLATE compression" (we write STORED, for byte identity) and that "RSMF files greater than 2 GB are
   not supported" (our structural rule allows a part's zip up to about 4 GiB, about 5.4 GB of `.rsmf`
   after base64). Decide (a) a 2 GB `.rsmf` limit as the structural rule, (b) DEFLATE with a pinned
   implementation, or confirmation from Relativity that STORED is accepted. Both are renderer bumps.
3. **Relativity licence:** only with one may the proprietary RSMF validator SDK run in CI (private
   .NET container, never redistributed); terms in phase-2.md, decision 4.
4. **Real Slack exports:** a Developer Program sandbox export and a free-plan workspace export, to
   become fixtures (`tests/fixtures/slack_exports/<name>/export.zip` + a hand-reviewed
   `expected.json`; `tests/integration/corpus/cases.py::real_exports`). Confirm every *(confirm on real
   export)* item in ADR 0014 then.
5. Product-owner confirmation of ADR 0013 decisions (a) and (c).
6. A cloud VM: the 1M soak, a 5 GB export ingestion, a real multi-GB native, `measure_report.py` at
   1M units (BACKLOG).
7. ADR 0017 (worker images) is accepted; M16 step 3 builds the first part of it for reports (image,
   toolchain id, registry entry); the RSMF side stays unscheduled.

## Decisions (all recorded)
- ADR 0013 (accepted): client-owned connections, workspaces modelled now, fixed roles, audited reads.
- Phase 2: the eight decisions in `docs/plans/phase-2.md` ("Decisions (2026-10-02)").
- ADR 0015 §9-§22: every RSMF decision and implementation note, in order (§18 and §19.14 are the
  2026-10-05 review decisions; §22 the review of the natives build).
- ADR 0018 (accepted 2026-10-06): M16, with D1-D14 folded in and the S1 results (§5.10).

## Done: CI run 37412915073 diagnosed and fixed (2026-10-06, ADR 0015 §23)
- `test_sigkill_of_the_render_worker_during_planning` failed once in CI (render `failed`). Root
  cause, a product bug: `render_slice` ran on the event loop, so a heavy slice (10,001 events: about
  1.5 s per call here, more under load) starved the heartbeat ticker; Temporal timed out LIVE
  attempts and every retry starved the same way until the attempts ran out. Reproduced locally
  (4 of 10 runs under CPU contention fail exactly like CI). Fix: `render_store._render_slice` runs
  the renderer in a worker thread. The test also had a nondeterministic kill point (it killed on
  "status rendering", which often landed in `begin_render`): it now kills at the `planning` barrier,
  on the small crash-matrix job (the 10,001-event job made its duration depend on host load).
- New: `test_slice_rendering_longer_than_the_heartbeat_timeout_keeps_heartbeating` (a releasable
  synchronous barrier `render_slice`, `CrashHooks.block`, spinning in pure Python like the
  renderer, holds the render in the thread for more
  than twice the heartbeat timeout with one attempt allowed); mutation `slice_render_on_event_loop`
  (round `ci-heartbeat`) is caught.
- Rule in CLAUDE.md: never re-run CI to green without a diagnosis; intermittent = bug. CPU-bound work
  in activities runs off the event loop (applies to the M16 PDF render: ADR 0018 §6).
- Reproduction tooling (not committed, easy to rebuild): a loop that runs one test N times with
  `--basetemp` per iteration and optional busy-loop processes for CPU contention (on macOS never
  `seq 1 $N` for a count that can be 0: it counts down and starts 2), and a script that
  lists every activity's recorded attempt and previous failure from the Temporal histories
  (`client.list_workflows("WorkflowType = 'RenderWorkflow' ...")` + `fetch_history`). Note: Temporal
  records only the START of the attempt that ended an activity; earlier attempts show up as its
  `last_failure` (and as "Activity cancelled" in the worker log).

## Done: M16 design (2026-10-05/06)
- `docs/plans/m16.md` (proposal), the review decisions D1-D14, spike S1 (PDF byte identity across 20
  runs and two amd64 hosts with the same image; font isolation proven with a probe font and host
  fonts; PDF/A-2u passing veraPDF after moving CJK to TrueType), ADR 0018 accepted.

## Done: M15, RSMF renders (ADR 0015, ADR 0017)
- Pure renderer `edisc_renderers.rsmf` (slices per conversation-day in UTC or a matter time zone,
  10,000-event parts, deterministic EML and STORED zip through `edisc_custody.zipwriter`, vendored
  schema validation), loader and storage (`edisc_worker.render_loader`, `render_store`), the
  `RenderWorkflow` with the render's own custody stream (`edisc_worker.renders`), renders API, render
  packages (`edisc-render-package/3`) verified offline, the download endpoint, render episodes
  (`unroutable`, `sealing_stuck`), version-keyed render queues, the synthetic corpus with an oracle,
  the crash matrix with real SIGKILLs.
- §11 natives (renderer 1.3.x): attachments over `RenderOptions.external_over_bytes` (identity,
  1 MiB..4 GiB) and, by size, whatever keeps a part's zip free of ZIP64 travel next to the `.rsmf` as
  natives (server-side copy from the pinned evidence version, one verification read,
  `render_natives`, `natives_root` per batch, package `natives/`, natives API). The entry count splits
  parts instead.
- §22 review (this session): unambiguous placeholder names (renderer 1.3.1); concurrent writers of
  one native (abort-all only under the per-native lock, a re-list after it) and a race found in
  `.rsmf` production writes, fixed by the same per-key lock; one test per retention route; the
  mutation harness `scripts/mutation/` (84 breaks, all caught); dummy connector 0.4.0 with the full
  spec pinned in its golden. Goldens: `tests/golden/rsmf/1.3.1_unicode-15.0.0_tzdata-2026e_dummy-0.4.0/`
  and `tests/golden/rsmf-corpus/<same key>/` (older generations kept as history).
- Commits: `fab31ac` (step 4) ... `0845dfd` (§11 natives), then the §22 review commit (see `git log`).

## Done: M14, Slack export ingestion (ADR 0014, accepted with R1-R7)
- **M14.1** hardened streaming ZIP reader `edisc_custody.archive`, property-based fuzzing (R1, R5).
- **M14.2** pinned-version S3 range source with coalesced reads (R6; 0.8 requests per 1,000 entries).
- **M14.3** migration 0018 (`slack_exports`, `export_upload_parts`, `export_entries`,
  `export_conversations`), upload API with Content-Digest parts and audited tenant-admin limit
  overrides, `ExportIngestWorkflow` (hash, lock, R7 rejection, streaming validation, tier detection,
  findings, credential-less `slack_export` connection), package `edisc_connector_slack_export.layout`.

- **Retention extension job** (before M15, per review): matter evidence and validated exports of open
  clients, matter/client closing (migration 0019), ADR 0002 "Who owns retention".

- **M14.4** synthetic exports from the oracle (`edisc_connector_dummy.dialects.slack_export` + its own
  ZIP writer) with real-world variants (macOS re-zip, wrapper folder, ZIP64, data descriptors, name
  encodings), all accepted and reported; raw name bytes stored (migration 0020); range reads measured
  (docs/runs/2026-10-02-export-range-reads.md).

- **Reversible closing** (review 2026-10-02): tenant-admin reopen, immediate re-lock of lapsed evidence,
  `retention_gaps` + `audit.retention_gap` (migration 0021).

- **M14.5** export collection: connector, `archive_entry` evidence, export dialect, thread index,
  file links (rate-limited, host allowlist, gaps with reasons), `matched_against_archive` /
  `completed_against_archive` with the caveat in the API; cross-source identity (`item_source`).
  Review fixes (2026-10-03): file links follow redirects with per-hop allowlist, resolved-address
  checks and IP pinning (`file_links`); item workspace per conversation for Grid (migration 0023,
  `Connector.item_workspace`); dummy connector refused outside local/test/ci (`edisc_connector_dummy.guard`).
  `apps/web/` is the frontend from another session, committed on its own branch: never commit, modify
  or run it from here.

- **M14.6** custody package format `edisc-custody-package/2` (the verifier accepts /1 and /2): export
  zips embedded (`objects/<sha256>`, streamed) or referenced by SHA-256 (`export_package(...,
  archives="reference")`, `edisc-verify --archive <path>`); the archive hash is checked before any
  entry is read; entries found by exact name bytes, duplicates rejected, CRC-32/compressed size compared
  with the record, decompressed under the export's recorded limits, SHA-256/size checked, then item
  fragments (`edisc_custody.package_archives`).
- **M17 backend plan** (proposed, amended after review, not implemented): docs/plans/phase-2.md.

- **M14.7 crash matrix** (`tests/integration/api/test_export_crash_matrix.py`): a crash at every
  boundary of lock and validation (10 points) and of collection from an export (4 points x 2
  occurrences) resumes to the clean result; one real SIGKILL of the export worker process in the middle
  of the day-file index, then a new worker. Seams: `ExportIngest(hooks=CrashHooks)`.

## How to run what matters
- `make check` (lint, typecheck, unit). Integration only on the ephemeral stack:
  `MIN_FREE_GB=12 make test-env-up`, then `make test-integration-only TESTS=...`, then
  `make test-env-down`; the full suite on a fresh stack is `MIN_FREE_GB=12 make test-integration`
  (about 20 minutes here). This laptop often has 13-16 GB free; the stack uses under 1 GB.
- Mutation checks: `uv run python scripts/mutation/run.py --kind unit` (2 minutes), `--kind
  integration` on the test stack (10 minutes); README in `scripts/mutation/`.
- Goldens: `EDISC_RECORD_RSMF=1` (renderer), `EDISC_RECORD_CORPUS=1` (corpus, including
  `tests/integration/acceptance/test_render_batch_boundary.py`) only after a deliberate renderer or
  dummy version bump; never in CI; never overwrite.
- Render suites: `tests/integration/renders`, `tests/integration/corpus`,
  `tests/integration/api/test_corpus_export.py`, `tests/integration/api/test_renders.py`,
  `test_render_packages.py`, `test_render_natives.py`, `tests/integration/custody/test_render_package*.py`.

## Other sessions and branches
- The frontend is on branch `feat/web-ui` (another session). Never commit, modify or run `apps/web` from
  a backend session. It lists the contracts it assumes in `apps/web/src/api/pending.ts`; ADR 0016 adopted
  them.
- Several Claude sessions have worked in this tree. Start with `git status` and `git pull`, stage
  explicit paths only (never `git add -A`), and never restore files with `git checkout` (it destroys
  uncommitted work).

## Gotchas learned (read before changing things)

**Environment:**
- **Disk:** locked test evidence cannot be deleted. Integration tests only ever run on the ephemeral
  `edisc-test` stack (`make test-integration`, or `make test-env-up` / `test-integration-only` /
  `test-env-down` while iterating). Targets refuse below `MIN_FREE_GB` (15). On this laptop free space
  swings with macOS swap; I ran the test stack with `MIN_FREE_GB=12` when needed (the stack uses under
  1 GB). Never run tests against the dev stack.
- **macOS:** Docker Desktop's VM can go read-only when the host disk fills. The fix was quitting Docker,
  deleting `Docker.raw`, and `kill -9` on a stale backend. The repo moved to
  `~/projects/kickoff`, which broke the `.venv` shebangs; `rm -rf .venv && make sync` fixed it.
- **Elasticsearch:** its image pull from docker.elastic.co stalls here. It is in an opt-in compose profile
  (`make up-search`) and nothing uses it.

**PDF (spike S1, step 3):**
- Under Rosetta, a PDF child over its RLIMIT_AS dies of a failed mmap or a GLib thread abort (SIGTRAP),
  not a Python MemoryError: tests accept any death as `ReportPdfRenderError`. Worker containers on
  Docker Desktop reach the test stack via host.docker.internal (Linux: `--network host`).
- `.dockerignore` keeps `apps/web` (another branch) and `docs`/`spikes` out of the image context.
- **Docker Hub pulls can stall for 20+ minutes here** (0 B progress, then finish). Do not cancel a
  build stuck at `FROM`; once the base is cached, rebuilds are fast. `snapshot.debian.org` was fast.
- **fontTools writes the current time into `head.modified`** unless fonts are opened with
  `recalcTimestamp=False` (done in `fetch_fonts.py` when instancing variable fonts).
- **CID-keyed CFF CJK fonts (`noto-cjk` SubsetOTF) fail PDF/A** after WeasyPrint's HarfBuzz subsetting
  (veraPDF 6.2.11.4.1, 6.2.11.5, 6.2.11.8), although poppler draws them. Use the TrueType
  `google/fonts` CJK variable fonts instanced at build.
- **Bidi controls must be replaced, not annotated:** a marker placed after U+202E is itself reversed.
- WeasyPrint names subset fonts with a 6-letter tag from an MD5 of the font description (stable);
  embedded font names have hyphens (`Noto-Sans-Linear-B`), so search font names, not raw bytes.
- Japanese kana/kanji render with the SC font (the JP font was unused, so it was removed): Han
  unification, names carry no language tag. A documented limitation (ADR 0018 §5.4).
- A 6,000-row, 234-page report takes about 30 s alone (47-84 s with four concurrent) and about
  1.2 GB RSS per render: hence PDF concurrency 1 and a 3 GiB container per report worker.

**Postgres:**
- **Health check:** it must use TCP (`pg_isready -h 127.0.0.1`); the socket check passes during first-boot
  init and caused CI races.
- **Stale statistics during bulk loads** flip plans to day-wide scans. Select linked items by id and filter
  in Python (CLAUDE.md). Bulk tables re-analyze at 2%.
- **Owner and RLS:** FORCE RLS also applies to the migration owner. Backfills relax it inside the migration
  transaction only (see 0015).
- **Definer functions:** cross-tenant lookups are SECURITY DEFINER owned by the sweeper login. Grant EXECUTE
  before `ALTER FUNCTION ... OWNER`; afterwards the owner cannot grant.
- **No deletes:** the app role may not DELETE. End records instead (`revoked_at`, `removed_at`). A test checks
  that no tenant table is deletable and that every tenant table has FORCE RLS and test coverage.

**Temporal:**
- **Patches:** changes that alter commands need `workflow.patched` plus a new golden generation.
  `keep-early-wake` is active (ADR 0012 registry).
- **API tests keep the production 120 s job poll on purpose,** so a lost wake signal shows up as a timeout.
- **The sandbox re-imports workflow modules:** no file I/O at import in modules that define workflows
  (`tests/unit/temporal/patched_workflows.py`).
- **Never `--no-verify`.** The workflow test helpers use the in-process worker; the 50k test uses real
  worker processes (`python -m edisc_worker --queue ...`).

**Evidence and custody:**
- MinIO ignores If-None-Match on CopyObject; PutObject honours it. Hence the advisory lock.
- `evidence_objects.upload_id` is write-once: a retried upload for the same row is not recorded, so
  resume logic must LIST open multipart uploads at the key (see `write_native`), not trust the column.
- Two executors of one render can run at once (a zombie activity and its retry): every write keyed by
  content or name takes the advisory content lock (`EvidenceWriter._content_lock`).
- Tampering with insert-only tables in a test (e.g. `render_natives`): as superuser,
  `SET session_replication_role = replica` first; CHECK constraints still apply.
- `timedelta(0)` is falsy: use `is not None` checks for optional durations (a real bug, fixed).
- **Anchoring:** never write an anchor without winning the claim. One anchor per due point.

**Testing:**
- **Lone surrogates in test sources:** the Write tool turns `\ud83d\ude00` escapes into the real
  character (and non-ASCII escapes into literals); build surrogates with `chr()` or edit through a
  script, and check with `cat -v`.
- **uv and local packages:** a no-dev venv built from the tree reuses uv's cached wheel of a workspace
  member until its pyproject changes: pass `--refresh-package <member>` (the production-install test
  does).
- **Tests are not type-checked:** a signature change (e.g. `report_task_queue` gaining `glyph_set`)
  can break a test only the image job runs: grep the tests for every call site.
- **A loop block seen once (undiagnosed):** 265 ms in Starlette's `BaseHTTPMiddleware` during the
  first `export_lone_surrogates` run (the stack's first API requests after start-up); not reproduced
  in 11 later runs, 8 under 4-core contention. Reproduce it in a loop with CPU
  contention before calling it noise (CLAUDE.md rule).
- **Heartbeats come from the event loop:** anything CPU-bound in an activity must run in a thread;
  otherwise a live attempt is timed out and retried until it fails (ADR 0015 §23). The real
  SIGKILL tests use `heartbeat_timeout_seconds=3`, which makes such starvation show up in tests.
- **CI failure artifacts (ADR 0015 §24):** a failed integration run uploads
  `integration-failure-<run>-<attempt>` with, per failed test, the worker logs, the Temporal
  histories of the workflows it started and the custody streams it touched
  (`tests/integration/artifacts.py`). Still reproduce locally in a loop before fixing; never re-run
  to green.
- **The loop guard (ADR 0015 §24):** every test fails if the event loop (its own or a spawned
  worker's) was blocked > 250 ms (`EDISC_TEST_LOOP_BLOCK_MS`), with the blocked stack; functions run
  in threads are listed in `OFF_LOOP` in `tests/conftest.py` and fail a test if called on a loop
  thread. Measuring: `EDISC_TEST_LOOP_BLOCK_MS=50 EDISC_TEST_LOOP_BLOCK_MODE=report
  EDISC_TEST_LOOP_BLOCK_LOG=<file>` records every block as JSON lines without failing.
- **Mutation checks:** after writing a test, break the code it protects and confirm the test fails, through
  `scripts/mutation/` (it isolates the bytecode cache per run: two same-sized edits of one file within a
  second once loaded a stale `.pyc` and made a break look harmless). Several tests were vacuous until this
  was done.
- **Noisy laptop:** absolute timings vary up to 2x between runs. Use paired measurements (the scripts in
  CLAUDE.md, "Measurements").
- **API tests:** they scan every response for every credential they sent. Use `secret()` from
  `tests/integration/api/conftest.py` for any credential you pass.
- **Client authentication (step 3b):** `Resources` builds its `ClientSigner` eagerly from the
  settings and `box.kms`; a test that builds `Resources` without a KMS passes `client_signer`. The
  test IdP refuses exchanges without an assertion once `enable_login` registered our published JWKS
  (`TEST_IDP.refusals` says why); `enable_login(..., client_auth="none")` for a public client.
- **Dev IdP:** `EDISC_API_DEV_IDP` exists only for local/test/ci. Its key is cached; re-parsing it per request
  cost about 75 ms and once masked a measurement.
