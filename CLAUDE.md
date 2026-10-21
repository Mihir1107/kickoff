# CLAUDE.md: project rules for future sessions

eDiscovery collaboration-data **collection module** (client > matter > workspace, Relativity model).
It collects Slack/Teams/etc. communications defensibly and emits RSMF + native JSON + HTML + a collection report.
The plan of record is in `docs/ARCHITECTURE.md` and `docs/adr/`. Read them before changing the core.

## Non-negotiable principles (these override convenience)
1. **No silent data loss.** Reconcile expected vs collected per unit. Gaps fail loudly
   (`completed_with_gaps` / `completed_unverified` / `failed`, never `completed`). Never swallow
   exceptions in the collection path: no bare `except`, no `except Exception: pass`, no logging-and-continuing.
2. **Tamper evidence.** Every raw item is SHA-256 hashed at collection and stored in WORM (S3 Object
   Lock, COMPLIANCE mode). Application code never modifies raw data, and deletes it only through the
   destruction workflow: under its own storage identity, after the owner closed, retention lapsed on
   the store's clock, approved by a second, different principal, after the cooling-off, and never
   while a legal hold applies in the database or on the stored object; every deletion is recorded in
   custody and certified (M18, `docs/plans/m18.md` §12).
3. **Chain of custody.** Every action (connect, collect, write, render, export) appends a custody event
   to a hash chain: `event_hash = sha256(prev_hash + canonical_json(event))`. `custody_events` is append-only (DB triggers).
4. **Idempotency.** Key = `tenant_id + source + source_item_id + content_hash`. An edit is a new version, never an overwrite.
5. **Resumability.** Any job can be SIGKILLed anywhere and resumes from its last DB checkpoint with zero gaps and zero duplicates.
6. **Tokens stay with us.** No third-party integration platforms or hosted auth brokers. Tokens are
   envelope-encrypted per tenant, never logged, and never passed through Temporal payloads.
7. **Thin connectors.** Connectors authenticate, enumerate and fetch raw only. All interpretation goes in `edisc-normalizer`.
8. **Streaming, never loading.** Bounded chunks. Hash while uploading. Never hold a whole conversation/collection in memory.

## Layout
- `packages/*`: libraries (`edisc_core`, `edisc_db`, `edisc_evidence`, `edisc_custody`, `edisc_verify`, `edisc_connectors_base`,
  `edisc_connector_*`, `edisc_normalizer`, `edisc_renderers`). src layout, one uv workspace member each.
- `apps/api` (`edisc_api`): FastAPI. `workers/collection` (`edisc_worker`): Temporal workers, one task queue per source.
- `infra/docker-compose.yml`: Postgres, Redis, MinIO (object lock), Temporal + UI, Elasticsearch (optional `search` profile, off by default). Pinned by tag + digest.
  MinIO is the `pgsty/minio` community rebuild (upstream stopped publishing images). Temporal UI: http://localhost:8080.
- `tests/unit` (no services), `tests/integration` (real compose services, **never mock DB, S3 or Temporal**), `tests/golden`.

## Commands
```
make sync               # uv sync --all-packages
make hooks              # once per clone: pre-commit runs ruff lint, ruff format --check, mypy on staged .py
make up / down          # infra up (waits for health, runs idempotent init jobs) / stop
make up-search          # optional Elasticsearch (compose profile "search"; nothing uses it yet)
make up-ci              # subset CI uses (no UI, no Elasticsearch)
make nuke               # down + delete volumes; refuses unless EDISC_ENV=local|test|ci
make migrate            # bootstrap roles/schema as superuser (idempotent), then alembic upgrade head as owner
uv run python -m edisc_db.migrate downgrade <rev>   # explicit target only; never 'base' outside scratch DBs
make lint fmt typecheck # ruff, ruff format, mypy --strict
make test               # unit tests
make test-integration   # FRESH ephemeral stack (-p edisc-test, .env.test, other ports), tests, then down -v
make test-env-up / test-integration-only / test-env-down   # keep the test stack up while iterating
                        # up/up-ci/test targets refuse below MIN_FREE_GB (15) free disk
make production-readiness  # fails while anything refuses production (allow-list pending_frontend); READINESS_MODE=warn reports only
make test-report-pdf    # report image (amd64): in-image PDF tests, PDF goldens, veraPDF, worker OOM (needs test-env-up)
make verifier-standalone   # verifier wheel twice (reproducible), empty venv, compared with the in-tree one (needs test-env-up)
make worker / api       # Temporal worker (+ maintenance queue, sweeper schedules, exports, renders) / API
```
- **Every test run leaves its full output on disk.** Every make test target runs through
  `scripts/test_log.py`: `test-runs/<UTC time>-<label>/output.log` (everything printed, colour
  stripped, exit status), `artifacts/` (per failed integration test: worker logs, Temporal histories,
  custody streams, exactly as CI uploads), `phase-timings/`, and `on-failure.log` (the test stack's
  logs) when it failed; `test-runs/latest` is the newest; pruned to 20 runs and 1 GiB (`TEST_RUNS_KEEP`, `TEST_RUNS_MAX_MB`; the newest and any run still being written are never removed). Run tests
  through make (or the wrapper), never a bare `pytest` whose output only the terminal sees: a one-off
  failure must be diagnosable from what it left.

## API (M13, ADR 0013)
- `apps/api` (`edisc_api`): tenant from the Host subdomain (`tenant_id_for_subdomain`) + the token's IdP
  (`tenant_idps`) + an active principal. NEVER read a tenant id from a body, query or header. Unknown
  tenants and all auth failures get the same 401.
- Every route declares `openapi_extra=perm(P.X, E.<codes its handler answers>)` (or `public(...)` /
  `authenticated(...)`; codes only from the registry `edisc_api.error_codes.E`) and every `/v1` route calls `authorize(s, caller, P.X, Scope(...))`
  inside its `tenant_tx` (404 if the caller cannot see the target, 403 if it can but lacks the permission).
  Request models `extra="forbid"`; response models only (no ORM rows or dicts); cursor pagination
  (`edisc_api.pagination`) on every list.
- State changes: job actions are custody events in the job stream; everything else is
  `edisc_api.audit.record` (custody stream = tenant id) with `caller.actor` and the request id. Evidence
  content reads are audited BEFORE bytes are returned.
- Credentials only through `edisc_db.connection_tokens`; the API test suite scans every response for
  every credential it handed in.
- Nothing is deleted: memberships and role assignments are ended (`removed_at` / `revoked_at`). The one
  exception is destruction (M18): the content columns of destroyed items (text, names, derivations)
  are redacted by the `edisc_destroyer` role through one definer function; rows, ids, hashes and
  custody chains stay.
- No error path echoes a request value (ADR 0016 A8): 422 bodies come from `edisc_api.safe_errors`
  (type, sanitised loc, schema-only msg; never input/ctx); an unhandled error is a fixed 500 whose
  log line has types and frames, never the text; log lines carry `request_path` (static segments
  only), never the query; `ApiError` details are fixed texts or server state, never a value the
  request sent; our validators raise `PydanticCustomError("edisc_...", "<static>")`; worker texts in
  answers are `RecordedText`. `test_no_echo.py` fuzzes every route from the OpenAPI document.
- The contract (M17 step 4): `docs/api/openapi.json` is generated (`make openapi`) and committed with
  every API change (a unit test and CI's `api-compat` job fail on drift); operationId = function name.
  `scripts/api_compat.py` (pinned oasdiff + our rules) fails CI on a breaking change; an intended one
  needs `/v2` or a `docs/api/breaking-allowlist.yaml` entry (rule, operation, reason, and EITHER the
  frontend commit that ABSORBED it OR `status: pending_frontend` + `tracking: <file>.md#<anchor>`;
  never a commit that does not absorb it). While any `pending_frontend` entry exists,
  `make production-readiness` fails (enforce mode) and so do release tags (`v*`, CI job
  `production-readiness`) and the report image's production target (`report`); on main, branches
  and PRs the job WARNS (one annotation per item + a job summary) and stays green: red always
  means something.
  A new response enum value is not breaking (D9). Error bodies are published as sent (422:
  `ValidationErrorOut` / `UnprocessableOut`) and validated against the document at test time.
- Job streams (M17 step 5, `edisc_api.stream`): only the models of `edisc_api.stream_events`, only
  the statements in `stream.STATEMENTS`, nothing that reads evidence; ids follow the job's chain
  (no missed or repeated units across `Last-Event-ID`); passive authentication (never extends a
  session), re-checked <= 15 s as two halves (`SessionWatch.interval`: ask every bound/2, answers at
  most bound/2 old); bounded per-subscriber queues (overflow ends with `slow_consumer`); the replay
  is bounded (`EDISC_SSE_REPLAY_MAX`, `..._MAX_AGE_SECONDS`; beyond: ONE `reset` event).
  `connection` (the job's connection's status) goes out when it is not `active` at the start and
  on every change, right before the snapshot showing it, WITHOUT an id; the snapshot digest covers
  the connection's status (a reconnect neither misses nor repeats it). The stream reads only the
  connection's `status` (a unit test). A change to an event schema breaks `api_compat` (`x-sse-events`).
- Downloads (M17 step 5b, `edisc_api.downloads`): every content route passes its body through
  `guarded(...)` with a `Download(operationId, ids, size, read.seq)` and declares an exact
  `Content-Length`; the session (or a bearer principal) is re-checked between pieces, a cut is
  audited (`download_aborted`, bytes sent), anchored, then the connection is reset (`DownloadCut`).
  A new content route needs `guarded` + `GuardedResponse` + `still_permitted` (the read
  permission is re-checked too) and a cut test (`tests/unit/api/test_download_routes.py`). Every
  write is timed (`EDISC_DOWNLOAD_STALL_SECONDS`: a stalled client is cut, `stalled`). Nothing
  pooled is held while bytes flow: S3 by ranges (`edisc_evidence.worm.read_ranges`, never a
  GetObject body across a yield), no transaction across a write, and NO `BaseHTTPMiddleware`
  (`@app.middleware("http")` buffers bodies in its own task; plain ASGI middleware only).
- Lists (M17 step 3): a cursor is bound to its route, path and filters (`pagination.bind_cursors`,
  app-wide; mismatch 422 `cursor_mismatch`). A list shows exactly what the matching GET would
  authorize (`GET /v1/jobs`: a `job.read` role at the job's matter, client or tenant, as a join on
  role assignments, never a list of ids); a test compares the two per role.

## Browser sessions (M17 step 2, ADR 0016 + Amendments 1-2 + step-2 notes)
- A request carries the session cookie (`__Host-edisc_session`) OR a bearer token; both: 400
  `ambiguous_credentials` before either is evaluated. Only the SHA-256 of the 256-bit id is stored
  (`sessions`, FORCE RLS, guard trigger: identity immutable, revocation final, clock only forward).
  Validity is checked per request with the DATABASE clock; the activity write is at most once a
  minute and never past the absolute expiry; `request.state.passive` (SSE) never moves it.
- Cookie requests: `Sec-Fetch-Site` other than `same-origin`/`none` is 403 `cross_site` (GETs too:
  content routes have side effects); state changes need `X-CSRF-Token` (HMAC of the session id,
  keyring `EDISC_API_SESSION_KEYS`, kid-prefixed) AND Origin/Referer = the tenant origin. Responses
  get `Cache-Control: no-store`, `Vary: Cookie`. Bearer requests are exempt from CSRF.
- Sign-in (`edisc_api.routes.auth`, `edisc_api.oidc`): one sign-in IdP per tenant
  (`tenant_idps.login_client_id`); `return_to` validated, stored, never rewritten; the attempt is
  consumed in the statement that finds it; the PKCE verifier is DERIVED from the login cookie handle
  (never stored); ID token keys only from the registered `jwks_url`. The callback answers the
  bounce page (200 + meta refresh), never a 303 (A1; `test_sessions_browser.py` proves it in
  Chromium, Firefox and WebKit). A new session id at every sign-in; the browser's previous session
  is revoked `rotated` in the same transaction. Failures: fixed codes, audited, throttled.
- `require_recent_sign_in(caller, settings)` on every ADR 0016 §4 action (and principal
  deactivation) inside its transaction, before any change; its return value (`authenticated_at`)
  goes in that action's audit payload. A new sensitive route does the same and joins the table in
  `test_sessions.py::_sensitive_requests`.
- Deactivation and admin revocation end every session in their own transaction. Nothing is deleted.
- The IdP's groups claim only through `edisc_api.idp_groups` (sign-in and bearer): an overage
  (`_claim_names`/`hasgroups`) or, when roles depend on groups, an absent/malformed claim REFUSES
  (`groups_overage` / `groups_missing`, audited, alerted); never read as "no groups". An empty list is
  an answer.
- Client authentication at the IdP (M17 step 3b): `private_key_jwt` only, through
  `edisc_api.client_auth.ClientSigner` (ONE place builds client assertions; the Teams
  client-credentials leg reuses it). The platform key lives in KMS (`KmsSigner`), never in a setting,
  the database or a log; `EDISC_API_CLIENT_KEYS` names it (current first) with the certificate's fixed
  validity. `tenant_idps.login_client_auth = 'none'` only in local/test/ci. Published keys:
  `/.well-known/edisc-client-jwks.json` and `-cert.pem` on the base host. Real-tenant checks:
  `make test-real-idp` (skips visibly without `EDISC_REAL_IDP_*`, fails when half-configured).
- Test IdP: `tests/integration/api/oidc.py` (mounted on the API fixture's HTTP client) and
  `sessions_flow.py` (sign-in helpers, a cookie jar that withholds Strict cookies cross-site).

## Source installs (M17 step 6, ADR 0016 A2-A4, A7, step-6 notes)
- Slack and Microsoft connect ONLY through `edisc_api.routes.installs` (start: a browser session
  and `connection.manage`; the generic credential routes exist only in local/test/ci, D10). The
  tenant comes only from the signed single-use state (`installs.sign_state`: MAC over install,
  expiry, tenant, principal, nonce) and the row it names; the relay host (`oauth.<base>`) never
  reads anything else from the query. External ids come ONLY from the providers' authenticated
  answers (Slack's exchange, Microsoft's signed ID token `tid`), never the redirect, and bind
  through `bind_external` (one tenant per external id; moves only by `edisc_api.admin
  move-binding`). Install tokens: redactor first, journal first (`install_tokens`). Microsoft:
  the gate is the consent token (client credentials for the `tid`: that tid, our app, application
  roles); `wids` is extra confirmation, never required, never skipping a check. The token journal
  holds only sealed blobs (a CHECK) and is DELETED with its apply; a journaled install is finished
  by the worker's reconciler (`edisc_worker.install_recovery`), exactly once.

## Access lost (M17 step 8, ADR 0016 A6 + step-8 notes)
- A pause for re-authorization records its reason CLASS (`AuthenticationError.reason_class`:
  `credentials_revoked` / `credentials_expired` / `consent_withdrawn` /
  `platform_credential_expired`; the class rides in the error text, so workflows are unchanged),
  a deadline fixed at the pause (tenant `reauth_deadline_hours`, 24 h..90 days, default 168) and a
  warning time; one alert per job, one warning alert. It ends ONLY by re-authorization (resume),
  the deadline, or `POST /v1/jobs/{id}/finish-without-access` (`job.resume`, a reason, recent
  sign-in). The last two go through `edisc_worker.access_lost`: decide (one transaction,
  `job_pause_ended` in the chain), then close each unsettled unit `access_lost` in its OWN
  transaction with its `unit_reconciled` event, and only then `stop_reason = 'access_lost'`.
  Never finalize with a unit unsettled; a job with no work unit never finalizes clean. A refusal
  pauses only the credentials still in force: activities stamp the credentials they loaded
  (`activities.credentials_stamp`), the stamp rides in the error text, `pause_for_reauth` compares.
- `check-connections` (every 6 h): `Connector.check_credentials` through the limiter for every
  active connection with tokens; a source without a connector in the worker is `unchecked`, never
  fine. A new connector implements `check_credentials`.
- Test stacks shorten the deadline with `EDISC_REAUTH_DEADLINE_OVERRIDE_SECONDS` (test/ci only).
- The report states how each pause ended FROM THE CHAIN (`job_resumed` = `reauthorized` and who;
  `job_pause_ended` = `deadline` / `finished_without_access`, who, the reason given; else
  `not_ended`), report renderer 1.6.0; `edisc-verify --job-package` recomputes it (older reports
  in their own shape). The loader's `pauses_differ` compares how each pause ended.

## Directory snapshots (M17 plan §3.3; migration 0043; D12, D14)
- `GET /v1/connections/{id}/directory?kind=conversation|user&q=` NEVER calls the source: it serves
  the current COMPLETE generation (`directory_snapshots`, `directory_entries`). Not captured yet is
  `status: not_captured` with `items: null`, never an empty list. Pages <= 500 (D14), keyset on
  `(search_key, external_id)` (COLLATE "C", the search index; `q` is a range on the NFKC-folded
  prefix); the cursor names its generation, so paging stays in it after newer ones complete.
- Who (D12, the default pending the mentor): `connection.read` on the client, or `job.start` on the
  client or one of its matters; anyone else gets `connection.read`'s 404/403. Never reviewers or
  auditors (names of a workspace's users are personal data).
- Captured only by `edisc_worker.directory` (`refresh-directories` every 15 min over
  `directories_due`, and `edisc_api.directory_refresh.start` after an install; off in the API test
  settings): pages through `Connector.fetch_directory` (the limiter), parses with the PURE
  `edisc_normalizer.directory` (in a thread: `parse_page` is in `OFF_LOOP`), stores each page in ONE
  transaction with the checkpoint (`source_cursor` + `pages` must be what this executor read);
  the last page completes the generation in that transaction and supersedes all but the current and
  the previous one. Budgeted runs (`EDISC_DIRECTORY_REFRESH_BUDGET_SECONDS`) resume from the
  checkpoint. Refused credentials or an unreadable page: the generation `failed`, the previous stays
  served. Exports make no source call: conversations from `export_conversations`, users from the
  locked `users.json`. Names keep lone surrogates as replacement + code units; ids become stand-in
  keys (§28), never dropped.

## Slack exports (M14, ADR 0014)
- Upload: `POST /v1/clients/{c}/exports` → `PUT /v1/exports/{id}/parts/{n}` (Content-Digest sha-256) →
  `POST .../complete`. The `ExportIngestWorkflow` (queue `exports`, `edisc_worker.exports`) hashes the staged
  object, locks it (`EvidenceWriter.lock_staged`), audits, then validates the central directory of the
  LOCKED version. A declared SHA-256 mismatch rejects before any parsing (R7).
- Zip parsing only through `edisc_custody.archive` (pure, fuzzed; R5). Entries are read in local-header
  order through `CoalescingSource` (R6). Limits come from the export row (`slack_exports.limits`), never
  straight from settings: a tenant admin may override them per upload (audited).
- Collection: `edisc_connector_slack_export.connector` (queue `collect-slack_export`). Batches reference
  entries of the locked zip (`archive_entry` evidence, never a copy); items use `item_source = "slack"`
  so exports and live API collections share identities (ADR 0004 amendment). Never `git checkout` a
  file to undo a mutation check: restore from a copy (uncommitted work is lost otherwise).
- File links only through `edisc_connector_slack_export.file_links` (allowlist + https re-checked per
  redirect hop, resolved addresses must be global, connection pinned to the checked IP).
- Item workspace is per conversation (`Connector.item_workspace`; exports: the conversation record's
  team, else users.json). The dummy connector is refused outside local/test/ci (`edisc_connector_dummy.guard`).
- Layout/tier rules: `edisc_connector_slack_export.layout`. Format details marked *(confirm on real
  export)* in ADR 0014 stay provisional until the real exports are fixtures.

## RSMF renders (M15, ADR 0015; steps 1-5 and §11 natives done, renderer 1.3.1)
- Renders read normalized items and derivations, never raw pages (raw evidence only to embed file
  bytes, by pinned version). The renderer `edisc_renderers.rsmf` is pure (no DB/S3/clock imports; a
  test enforces it). A worker loader builds `SliceInput`s and streams evidence through a `FileOpener`.
- Byte-identical output for the same inputs AND renderer version: no clocks, fixed zip timestamps and
  order, STORED entries (deflate bytes depend on the zlib build; ADR 0015 §6), canonical JSON,
  boundary and Message-ID derived from the source hash. Time zones ONLY via `rsmf.runtime.load_zone`
  (pinned `tzdata` package, never system zoneinfo); Python pinned to the patch in `.python-version`
  (fixes `unicodedata`). Changing output bytes needs a `RENDERER_VERSION` bump (`rsmf/version.py`);
  goldens live in `tests/golden/rsmf/<golden_key()>_dummy-<version>/` (renderer + Unicode + tzdata +
  dummy versions), recorded with `EDISC_RECORD_RSMF=1` (never overwrites, refused in CI). Goldens are
  regression guards; correctness comes from oracles. The render corpus (`tests/integration/corpus`,
  oracle + coverage matrix + masked goldens via `EDISC_RECORD_CORPUS=1`) and the render crash matrix
  must stay green; a dummy connector change bumps its version and records new generations.
- Natives (ADR 0015 §11, §20, §21): an attachment over `RenderOptions.external_over_bytes` (part of the
  render identity, 1 MiB..4 GiB, never from settings), then the largest while `ZipSizer.needs_zip64()`,
  leaves the zip as `{file_id}_EXTERNAL.txt` (pinned bytes) + `edisc.file_external`; the renderer
  never reads it. The entry count never externalizes: it splits parts (`MAX_PART_ENTRIES`). The
  renderer writes zips only through `edisc_custody.zipwriter`. Natives are copied SERVER-SIDE from the
  pinned evidence version (`EvidenceWriter.write_native`, content lock, one verification read), one
  per (render, SHA-256), before the batch that first references them. `write_production` is under the
  same per-key lock (two executors of one render are possible: a zombie attempt and its retry); `render_natives` + each
  batch's `natives_root`. The file leaf and natives checks follow the renderer version in
  `render_started` (`edisc_custody.render_files.has_natives`).
- Reconciliation: every in-scope message appears as exactly ONE event across the render's files, plus
  marked context events. `Reconciler` fails loudly; the loader must pass `finish()` the count and
  `subject_digest` derived from the job's in-scope links, never from rendered output. Each referenced
  thread root goes in `SliceInput.roots` or `missing_roots`; anything else raises.
- Loader (`edisc_worker.render_loader`): by id, per conversation; only sealed jobs. Every page or archive
  entry behind a rendered item is read by pinned VersionId and checked (registry SHA-256 and size, each
  item's `raw_hash` at `json_path`, `derived_hash`) BEFORE the slice reaches the renderer, outside DB
  transactions. Files stream by pinned version and are checked as they pass.
- Conversation metadata: versioned `conversation_snapshot` items (`{ws}/{conv}#conversation`) from the
  directory unit (dummy connector 0.2.0; a hard requirement for the Phase 3 live connector). Message
  derivations carry `files` ([id, name, mimetype], normalizer 0.2.0) so placeholders use the message's
  own file name. Every participant/conversation name stays searchable (`edisc.known_name`).
- Storage (`edisc_worker.render_store`): pass 1 reconciles with nothing written; pass 2 writes
  `production` evidence (`EvidenceWriter.write_production`, origin `render`) tied to the rendered job.
- Custody (`edisc_worker.renders`, ADR 0015 §14): each render has its own stream (stream id = render
  id; every event has `render_id` set and `job_id` NULL). `render_started` references the sealed job
  (id, final head, seal anchor key + VersionId listed from S3, completeness basis, versions, options);
  or `render_refused` is the only event. Files go in bounded `render_files_batch` events (Merkle root
  over `render_files.FILE_FIELDS`); `render_completed` carries totals + root over batch roots; then the
  seal + `audit.render_*` in one transaction. Never append to a sealed job chain.
- Render steps are fenced by the render's STATUS, moved in the same transaction as the event; a batch
  commits only when `batches_done` equals its index (never fence on a value that can repeat: see the
  ABA fix in ADR 0006). Anything that fails for good goes to `fail_render` (retried without limit).
  Conditions are `render_episodes` (`unroutable`, `sealing_stuck`): one open per kind, one alert per
  episode, closed ones kept (ADR 0015 §16); `check-render-routing` (DescribeTaskQueue) opens
  `unroutable`. Renders run on the queue of their recorded versions (`render_task_queue`); workers poll
  their own runtime's queue; the version check in the activities is only the safety net (ADR 0015 §15).
- A deleted message's reactions render only as `edisc.reactions_before_deletion` (history), never as
  RSMF `reactions`. `EDISC_TEST_RENDER_BARRIER` (test/ci only) blocks a render worker at a crash point
  so tests can SIGKILL it there.
- One live render per (job, options hash, renderer, Unicode, tzdata versions); failed/refused do not
  count. API: `export.create` / `export.read` (matter managers, tenant admins), reads `custody.read`.
  `require_recent_sign_in` is the ADR 0016 §4 hook (no-op until M17). The generic evidence content
  endpoint never serves `production` rows. Render anchors and productions carry `render_id`;
  retention resolves render -> job -> matter. `edisc-verify` verifies render packages
  (`edisc-render-package/3` with `natives.jsonl` and `natives/`, /1 and /2 still accepted; a directory
  or the zip in place; `--file` for outputs and natives, `--job-package`); it reads each `.rsmf`'s
  native references through `edisc_custody.rsmf_check`, never loading the file.
- Package download (`GET /v1/renders/{id}/package`, ADR 0015 §19): manifest from the records first
  (`plan_render_package`, no object read), audit committed AND force-anchored (`audit.anchor_now`)
  before any byte, then ONE streaming pass (`package_members`, shared with the directory export)
  that checks every entry against the manifest and aborts with `audit.render_package_aborted` + an
  alert. Zips only through `edisc_custody.zipwriter` (STORED, fixed metadata, data descriptor with
  the streamed CRC on every entry, ZIP64 where needed); two downloads must be byte-identical.
- Every route that returns evidence bytes (evidence content, render file, native, package) commits its
  audit AND calls `audit.anchor_now` before the response starts; tested at the first byte
  (`tests/integration/api/first_byte.py`). A new content route must do the same and add that test.
  `edisc-verify` is strict (unlisted files fail); `--tolerate-os-metadata` only for directories.
- The package's `anchors.jsonl` stays LISTED from S3 (survives a compromised DB); any difference from
  the DB anchor rows is served anyway, recorded (`audit.render_package_anchor_divergence`) and alerted.
- Every manifest is validated against the vendored `rsmf_schema_2_0_0.json` (SHA-256 pinned, format
  checks on). The Relativity validator is not used until the licence is confirmed.

## Collection report and preview (M16, ADR 0018; complete: model, HTML, PDF, loader, stream, API, preview, docs)
- Read ADR 0018 in full (its §19-§30 implementation notes, and §31: every deviation from the decision)
  before changing M16 code; `docs/HANDOFF.md` has what is next. `docs/VERIFYING.md` is the guide for
  third-party experts: a change to what `edisc-verify` checks or recomputes updates it. The report has its own custody stream (a sealed job chain is
  never appended to); facts come from the verified job chain first, database disagreements are
  reported as divergences, never resolved silently.
- The pure model is `edisc_renderers.report.model` and the pure HTML builder `edisc_renderers.report.html`
  (no DB/S3/clock: the purity test covers both; the offline verifier imports the model). `report.html`
  renders `report.json`: no template engine, the only text path is `escape`; every user string is
  revealed (bidi controls and HTML-invalid code points -- NUL, C0 but tab/LF/CR, DEL, C1,
  noncharacters, lone surrogates -- REPLACED by a marker; other Cf/Zl/Zp and tab/LF/CR kept + marker),
  escaped and `<bdi>`-wrapped. A marker is an ELEMENT (`<span class="cp">U+XXXX</span>`), never
  text. Pages must parse under html5lib STRICT. One constant inline `<style>` whose SHA-256 is in the
  page CSP; byte-identical per renderer version (`REPORT_RENDERER_VERSION`, bump on any byte change;
  goldens `tests/golden/report/<version>_unicode-<u>/`, recorded with `EDISC_RECORD_REPORT=1`, never
  overwritten, refused in CI). Above 1,000 conversations `conversations.jsonl` is written and named. The loader `edisc_worker.report_loader` streams every file;
  everything a report states that can change after the request goes into the write-once snapshot
  (renders, retention gaps, lock settings, the audit head, the job's own evidence statistics).
- Lifecycle in `edisc_worker.reports` (status fences in the same transaction as each event, like
  renders); files via `EvidenceWriter.write_report_file`; `report_generated` root via
  `edisc_custody.report_files`. Episodes (render and report) live in `production_episodes`.
  `ensure-job-reports` creates a report for every sealed job once and flags missing ones.
- PDF (ADR 0018 §22): bytes are promised only inside the pinned linux/amd64 report image
  (`deploy/report-image/Dockerfile`; fonts by `fonts.lock`, fontconfig isolated, Debian snapshot).
  The render runs in a CHILD process (`python -m edisc_worker.report_pdf`: RLIMIT_AS, oom_score_adj,
  minimal env, stdin/stdout only), never a thread; `render_pdf` kills it on timeout/cancel and any
  failure is the retryable `ReportPdfRenderError`. The PDF is rendered from the STORED report.html
  read back by VersionId. The toolchain id (`edisc_worker.versions`) is measured with the child's
  interpreter; outside the image it is `none` (local/test/ci only: no PDF). Glyph coverage lives in
  the HTML (`report/fonts/coverage.txt`, regenerate with `scripts/report_font_coverage.py`).
  `make test-report-pdf` (needs `make test-env-up`) builds the image and runs the in-image tests
  (`tests/unit/report_image`, skipped elsewhere with a visible reason), veraPDF and the worker
  OOM/heartbeat tests; PDF goldens `tests/golden/report-pdf/<key>_toolchain-<id12>/` are the image's
  admission gate (record by hand with `EDISC_RECORD_REPORT_PDF=1`, never in CI, never overwrite).
  It also runs the `image` kind of mutation breaks INSIDE the image (`scripts/mutation/run.py --kind
  image` edits the installed code there).
- Lone UTF-16 surrogates (ADR 0018 §23): SOURCE data is hashed with `canonical_source_json` (lossless
  `\uXXXX` escapes, = `canonical_json` for valid input); everything stored or published is valid
  Unicode via `edisc_core.lone_surrogates` (replacement text + `lone_surrogates`: JSON Pointer -> exact
  UTF-16 code units; `recorded_text` for error texts, `error_utf16` in the event). Never put a raw
  lone surrogate in a custody event, a DB row, a report or an RSMF; never hash source data with the
  strict form. Writing Python test sources with the Write tool turns `\ud83d\ude00` escapes into
  the real character: build surrogates with `chr()`.
- The report identity includes `glyph_set` (SHA-256 of the coverage ranges, §24): queue
  `reports.r<renderer>.p<toolchain12>.u<unicode>.g<glyphs12>`, golden key `..._glyphs-<g12>`.
- API (§27): `report.read` every role, `report.create` tenant_admin + matter_manager; manual requests
  need a reason and always create a new report; every content route (report file, report package,
  job package `GET /v1/jobs/{id}/package`, `export.read`) audits + `anchor_now` before the first byte.
  Packages: job `edisc-custody-package/3` (`edisc_custody.job_export`, deterministic), report
  `edisc-report-package/1`; `edisc-verify <report pkg> --job-package <job pkg or zip>` recomputes
  `units.jsonl` byte for byte and the sections derived from the chain (`edisc_renderers.report.recompute`).
- Hostile export values and unit facts (§28): a NAME holding a lone surrogate is text (stored as
  replacement + exact code units, `export_conversations.exact`); a STRUCTURAL value (conversation,
  team or workspace id, `ts`, `thread_ts`) may be refused as a key but its units must be GAPS with a
  specific reason (`work_units.gap_reasons`, `UncollectableUnitError`, `RawBatch.gaps`), never
  missing, never clean. New jobs record every unit fact in the chain (`unit_event_format` 2: details
  in unit events, `units_unsettled` at the end); the format is decided once per job by
  `job_started` (`facts_for_format`). `units.jsonl` must stay recomputable byte for byte from the job
  package (`edisc_renderers.report.recompute`): a new column the report states goes into the unit
  events first. Report order columns are COLLATE "C" (never depend on the DB locale).
  `--job-package` also computes the clean verdict from the chain alone (`chain_verdict`): a report
  stating clean when the chain does not FAILS; the other way round passes and prints the report's
  reasons. `job_started.normalizer_versions` + each batch's `normalizer_version` (new jobs) are
  what the report states and the verifier recomputes (ADR 0018 §30). The job
  package verifier is strict and reads zips in place, like the render and report verifiers.
- Export jobs (§32): `job_started.export` records the export's declared plan, detected tier, blind
  spots, tier warnings and validation findings (`pipeline.export_record`); the report states them
  from the chain (`model.export_section`, UNKNOWN for older jobs), the HTML has an Export section,
  the verifier recomputes it. Corpus: one export case per tier, and `export_full` on the tailored
  collation database too (`api.conftest.make_api` builds the API over any database).
- Preview (§29): `GET /v1/jobs/{id}/preview` (`evidence.read`) loads ONE day through
  `RenderLoader.day_slice` (items by their own time on the 0034 index, links by primary key, roots by
  exact id; never the whole conversation), renders with the pure `edisc_renderers.preview` (same
  sanitiser rules as the report, no glyph coverage; links only to `/v1/evidence/<uuid>/content?
  purpose=preview` and same-page fragments), audits `preview_read` with the output SHA-256 and
  anchors before the first byte; every refusal is `audit.preview_refused`. The no-remote-fetch test
  drives Chromium through Playwright (pinned; CI installs Chromium in the integration job).
- `make test-production-install` (CI job `production-install`): the API and every worker type start
  from a no-dev, non-editable install. A runtime import of a dev-only package fails it.

## Legal hold (M18 steps 3-4, ADR 0019 §4; migration 0047)
- "Is it held?" has ONE answer: the SQL functions `matter_held` / `client_held` /
  `client_directly_held` / `evidence_held` (invoker rights, RLS applies), through
  `edisc_custody.holds` in Python. Never re-derive it. A matter is held by a hold on it or its
  client; a client by a hold on it or any of its matters (it cannot close); an object through ANY
  owner (its job's matter, any job that linked an item behind it, its render's/report's job, a job
  that read an entry of its archive), or as the archive of an export of a client held directly.
- A hold blocks closing (API 409 `legal_hold` AND trigger EA005 on any path), retention shortening
  (EA004) and destruction (step 6 must call `evidence_held` per object and refuse on the S3 flag);
  never new jobs, renders, reports or reads. Retention extension keeps held evidence locked whether
  the owner is open or closed, past its retention date.
- Release = two different tenant admins (`legal_hold_release_requests`, a CHECK forbids the
  requester approving; 72 h; refused with fewer than two active user admins, never an operator
  path) and a cooling-off (`tenants.hold_cooling_off_days`, 7..90). Placement, request, approval,
  expiry, release: tenant-stream custody events anchored with `anchor_now`, plus an alert.
- S3 flags: `LegalHoldWorkflow` on queue `holds`, `python -m edisc_worker --holds` ONLY (hold
  identity for flags, app identity for anchors; it refuses every other queue). Progress counts only
  what the store confirmed; a missing version or a flag that disagrees with the database is an
  incident (event + alert), never fixed on the way. Clearing commits its intent
  (`clear_requested_at`) before any OFF call and never clears a version another active hold
  covers. `flag-held-objects` (15 min) re-runs passes; sealed jobs are read once per hold.
- Database rules of migration 0047 are tested on a database migrated in the test
  (`tests/integration/db/test_hold_rules.py`): a mutation of the migration must fail there.

## Conventions
- Python 3.12, `uv` only (no pip). Add deps with `uv add --package <member> <dep>`.
- mypy `--strict` on all source (packages/apps/workers). ruff is the formatter and linter.
- All timestamps are timezone-aware UTC (`edisc_core.time`). Naive datetimes and nonexistent (DST-gap) local times
  raise; nothing is ever assumed to be UTC. Use `UtcDatetime` for Pydantic fields. ruff DTZ enforces the rest.
  Exception: day *boundaries* in a timezone use `local_day_bounds`/`resolve_wall_time`, which resolve DST gaps to the
  earliest valid instant at/after local midnight (never raise). Day slicing defaults to UTC (`day_bounds`).
- Logging: `edisc_core.logs.configure_logging()` at process start; register decrypted secrets with
  `edisc_core.redaction.register_secret()`. Redaction covers nested fields, exception text and tracebacks.
- IDs are UUIDv7 (`edisc_core.ids`).
- Hash inputs use RFC 8785 canonical JSON (`edisc_core.canonical`), never `json.dumps`. Vectors + Node cross-check
  live in `tests/golden/jcs/`; keep non-ASCII in golden files as `\\u` escapes so editors cannot normalize them.
- Workflows carry IDs and small cursors only. Raw data, tokens and large payloads never enter Temporal history.
- The DB checkpoint is the source of truth for resume, not Temporal heartbeat details.
- Workflows (ADR 0012): `edisc_worker.workflows` imports only `contracts` (sandbox-safe) and calls activities BY NAME.
  Activities wrap `Pipeline` and raise only classified `ApplicationError`s (`edisc_worker.errors`). Any change
  to workflow code that alters commands goes behind `workflow.patched`; goldens in `tests/golden/temporal`
  (re-record: `EDISC_RECORD_HISTORIES=1 make test-integration TESTS=tests/integration/worker`); remove a patch
  only when `scripts/temporal_patch_check.py` exits 0. Active patches are listed in ADR 0012 ("Active workflow
  patches"); a new golden generation is recorded with `EDISC_RECORD_SUFFIX=-<patch>` next to the old one.
- Per-batch writes (items + job_items + custody event + checkpoint + counts) happen in ONE transaction
  (`edisc_worker.pipeline.Pipeline.process_batch`, ADR 0006): evidence and file downloads happen BEFORE it, the
  transaction starts with the checkpoint guard (moved cursor = no-op), links are inserted before the custody event
  (deferred FK) so the Merkle root covers exactly the new links. `in_scope` lives on `job_items`, not on items.
  Absence detection only for clean units against earlier clean collections of the same unit (ADR 0005).
- Several scopes per job (ADR 0005 amendment): units are the union over scopes (`work_unit_scopes` records
  coverage); `Pipeline.unit_scope()` gives the unit's merged range + most inclusive policy; in-scope = any range of a
  scope covering the conversation; collected counts are per conversation-day across the job's links.
- Queries that join a unit's links to items select items BY ID and filter day/type in Python: with stale statistics
  mid-load the planner otherwise scans a whole day's items (see docs/runs/2026-10-01-storage-throughput-breakdown.md).
- **Reads during and right after a bulk load are bounded by construction, never by the statistics** (ADR 0015
  §24.20). Right after a load the planner estimates the new rows at about one, and every index sharing the leading
  column looks equal. Per-row foreign-key checks and per-page queries then rescan everything loaded so far
  (quadratic: 643 M tuples for a 66,000-entry export on CI).
  - Analyze the loaded table before anything references it, through a ZERO-ARGUMENT definer function with a
    fixed, schema-qualified table list in static SQL (`analyze_export_entries`, `analyze_export_tables`,
    `analyze_collected_items`; migration 0045): the app role may not ANALYZE, and the caller chooses WHEN, never
    WHAT. A new one joins `tests/integration/db/test_analyze_functions.py` and `test_app_role.py`'s registry.
  - A collection refreshes its items' statistics as the job's inserted items double (`collection_jobs.items_inserted`,
    counted AFTER each batch's commit in its own one-statement transaction; `statistics_due`): a new tenant is
    otherwise estimated at one row and every FK check and per-batch lookup reads all its items so far. Never lock
    the job row inside a batch transaction: it deadlocks with transactions that lock it before a chain head.
  - Make a page query's good plan the only cheap one (`day_files_page`: `SET LOCAL enable_sort = off`; such a text
    runs ONLY through its guard, because cached generic plans ignore later setting changes).
  - Prove it on a database migrated from scratch in each statistics state, trap armed first
    (`tests/integration/db/test_stale_statistics.py`).
  - Assert WORK (tuples read), not time, in large tests.
  - Diagnose with `tests/integration/pg_diagnostics.py`: `AutoExplain` collects the plans the worker's own
    backends executed, and `WaitSampler` samples wait events. `auto_explain` is preloaded in compose and inert
    until enabled.
- Redis clients pass `driver_info=edisc_connectors_base.redis_driver.DRIVER_INFO`: without it redis-py reads
  package metadata on the event loop for every new connection (ADR 0015 §24.22).
- Schema changes: new Alembic revision in `packages/db/migrations/versions/` (hand-written SQL, run via `split_sql`),
  update `edisc_db.models` to match (drift test fails otherwise), add RLS + grants for any new tenant table.
- Tenant isolation: FORCE RLS on every tenant table. The app connects as `edisc_app` (not owner, not superuser,
  no BYPASSRLS) and sets `SET LOCAL app.tenant_id` per transaction via the single `tenant_tx()` helper, in activities too.
  Superuser is only for migrations and tamper tests.
- Evidence (`edisc_evidence.writer.EvidenceWriter`, ADR 0002 + amendment): pages -> job-scoped keys, single pass.
  Files -> content-addressed `t/{tenant}/files/sha256/...` (dedup per tenant): a key already `complete` in the registry
  is a dedup hit (no lock, no upload); a `destroying` or `destroyed` row never is: the key is written
  again as a new generation under the content lock; small files (<= `EDISC_EVIDENCE_SMALL_FILE_MAX_BYTES`) are hashed in memory and
  PUT once (If-None-Match, lock, ChecksumSHA256) under the advisory lock; large files stream to the staging bucket and
  are server-side copied. Source hash persisted before any WORM write. A page's files are written with bounded
  concurrency (`EDISC_EVIDENCE_FILE_CONCURRENCY`). Evidence hash = our own SHA-256, never S3's composite.
  Lock set at create; abort multipart on any failure; `recover_pending` at job finalize. No evidence on local disk.
- **Storage identities (M18 §3, ADR 0019 §2):** services never hold MinIO root (compose and
  `minio-init` only; tests tamper through `s3_root`). `edisc-app` (API, every collection/export/
  render/report worker; `EDISC_S3_ACCESS_KEY`), `edisc-hold` (`--holds` only), `edisc-destroy`
  (destruction only); policies `infra/minio/policies/*.json`, each with explicit Denies (MinIO
  does NOT evaluate bucket policies for IAM users: those are AWS-only, `infra/aws/`). Every process
  runs `assert_least_privilege` at startup (refuses anything that may read the bucket policy) and,
  outside local/test/ci, refuses another identity's credentials. MinIO authorizes a delete by
  version as `s3:DeleteObject`; markers are denied with an `s3:versionid` Null condition. The
  integration suite runs as `edisc-app`: a missing permission must fail a test. A new S3 call by
  the product needs its permission in the right policy file and `tests/unit/test_storage_policies.py`.
- **Export upload quotas (M18 §4):** per tenant (`tenant_quotas`, operator CLI `python -m
  edisc_api.admin quota` only), usage COMPUTED from `slack_exports` under a per-tenant
  `pg_advisory_xact_lock` in the create transaction (`edisc_api.quotas`); 409 `quota_exceeded`
  never echoes the declared size; `expire-export-uploads` rejects uploads past their TTL.
- Retention: rolling window `min(matter.retention_until, now + EDISC_EVIDENCE_RETENTION_WINDOW_DAYS)`, extended while the
  matter is active (extension job: backlog, required before production). Never lock for the full matter upfront.
  Dedup hits extend only below `EDISC_EVIDENCE_RETENTION_EXTEND_FLOOR_DAYS` (60), then to the target.
  `EDISC_EVIDENCE_RETENTION_OVERRIDE_DAYS` caps it and is honoured only when `EDISC_ENV` is local/test/ci.
  `EDISC_EVIDENCE_RETENTION_OVERRIDE_SECONDS` (test/ci only, never local) locks for seconds on the ephemeral test stack.
  Local/test/ci evidence buckets get ILM expiry (2 days, noncurrent 1 day); staging bucket expires in 1 day everywhere.
- Integration tests never run against the dev stack: test evidence is locked COMPLIANCE and can only be removed by
  destroying the volume. `.env.test` is generated (`scripts/make_test_env.py`); large temp outputs go under
  `--basetemp` in `$TMPDIR/edisc-tests`, wiped at start and end.
- Versions: `content_hash` = hash of the version fingerprint defined in ADR 0004, not raw bytes. Reactions never
  create message versions; they are `item_type=event` reaction-snapshot items linked to the message.
- Normalizer (`edisc_normalizer`): `slack.py` is PURE (bytes + prior state + file evidence -> `Derived`); all DB access
  is in `store.py` (load_prior / previously_observed / persist). Absence is never deletion (no_longer_observed);
  reverts are observed; derived records go to `item_derivations` per normalizer version. Download a page's files
  (`file_refs`) before normalizing it. Tests compare against the oracle in `tests/integration/normalizer/oracle.py`.
- Custody (`edisc_custody`): call `append`/`append_batch` INSIDE the tenant transaction; after commit call
  `anchor_if_due` (coalesced: an atomic claim on the head, one anchor per due point, ADR 0003 amendment; never
  anchor by writing the head without the claim). Lifecycle events and every N events are anchored; `seal_job_chain` at
  finalize; `sweep_anchors` (periodic) seals overdue/abandoned streams. `verify_chain` (DB) and `edisc-verify` (offline package, ADR 0008) share `ChainVerifier`. Anchors are always
  listed from S3 versions, never from the DB. Merkle = RFC 6962, leaves ordered by idempotency_key.
- **The offline verifier is its own distribution** (`packages/verify`, `edisc-verify`, module
  `edisc_verify`; M18 step 5, D12): it OWNS the offline code (canonical JSON, lone surrogates,
  jsonpath, idempotency keys, UTC formatting, the chain/Merkle/package/archive/RSMF-check readers,
  the report model and recomputation, the JWS) and imports only the stdlib, itself, `rfc8785` and
  `cryptography`, all pinned `==` and hashed (`packages/verify/requirements.lock`, `scripts/
  verifier_standalone.py lock`). The product imports FROM it, never the reverse; the old paths
  (`edisc_custody.chain`, `edisc_core.canonical`, `edisc_renderers.report.model`, ...) are
  `sys.modules` aliases for one release: new code imports `edisc_verify.*`. The JWS accepts exactly
  ES256 (`edisc_verify.jws`): never an algorithm from the token. Wheel reproducible
  (`SOURCE_DATE_EPOCH` = commit time). `make verifier-standalone` (CI job `verifier-standalone`,
  needs `make test-env-up`): built twice, installed in an empty venv, compared byte for byte with
  the in-tree verifier on real and tampered packages. Tests: `tests/unit/verify/`.
- `completed_unverified` is never presented as a clean completion (ADR 0005).
- Secrets: `.env.example` has placeholders only. Never commit `.env`. Wrap secrets in `SecretStr`.
- Dummy connector (`edisc_connector_dummy`): the golden dataset and its own oracle. Expected values in tests come
  from `Dataset`, never from collected data. Output is byte-identical per (spec, epoch); changing it, or ANY
  spec field or default, requires a `DummyConnector.version` bump and regenerating `tests/golden/dummy/small.json`
  (it pins the full effective spec with the version, so golden keys `_dummy-<version>` name one generator).
- Thread-parent policy (ADR 0011, PROPOSED): default `include_parent_and_thread`; do not change without sign-off.
- Rate limits (ADR 0010): every source request goes through `edisc_connectors_base.ratelimit.call_with_limits`
  with a `BucketKey(tenant, source, workspace, method)`; limits only from `EDISC_RATE_LIMITS`; raise
  `SourceThrottledError(retry_after)` on 429 so the whole bucket pauses. Never bypass the limiter.
- Connection tokens (ADR 0009): only via `edisc_db.connection_tokens` (store/load/refresh_tokens/rewrap_tenant_tokens)
  with a `SecretBox` over a `KmsClient`. Context = tenant + connection + purpose. Never pass tokens to Temporal,
  never log them, never add a new secret column without envelope encryption + context binding.
  Refresh responses are journaled in their own transaction before being applied; run `reconcile_token_refreshes`
  at worker startup. Prefer designs without refresh tokens (client credentials + certificate; Slack rotation off).
- Text order is code-point order whatever the database's collation (ADR 0018 §30): a column name
  the product orders by is COLLATE "C" in every table (migrations 0033, 0035), anything else is
  `COLLATE "C"` in the query. `tests/integration/db/test_collation.py` scans every `ORDER BY`;
  `tests/integration/corpus/test_collation.py` runs the corpus on a tailored ICU database.
- Row locks that must coexist with FK inserts referencing the row: use `FOR NO KEY UPDATE`, not `FOR UPDATE`.
- App/worker DB sessions set `lock_timeout` and `idle_in_transaction_session_timeout` (`EDISC_PG_*_TIMEOUT_MS`).
  A lock wait or abandoned transaction raises; `edisc_db.session.is_retryable_db_error` says whether to retry the
  whole unit of work. Never hold a transaction open across long I/O; long-held locks use a dedicated AUTOCOMMIT
  connection and waiters poll with `pg_try_advisory_lock` (see `EvidenceWriter._content_lock`).
- Tests have a 120 s timeout (pytest-timeout): a hang is a failure. Only the acceptance runs
  (`tests/integration/acceptance`) carry an explicit, larger `@pytest.mark.timeout`.
- **A test that fails intermittently is a bug until proven otherwise.** Never re-run CI (or a test)
  to green without diagnosing the failure first: get the logs and histories (a failed CI integration
  run uploads `integration-failure-<run>-<attempt>`: per failed test its worker logs, Temporal
  histories and custody streams, `tests/integration/artifacts.py`), reproduce it in a loop
  (with CPU contention), classify it (product bug or test bug) and fix the cause. A test bug is fixed
  with barriers or conditions, never with sleeps or loosened timeouts. Record the finding in the ADR.
- **Never loosen a guard, threshold or exemption to make a test pass.** A guard that fires (the loop
  guard, the churn report, a timeout, a budget, a verifier check) is a bug until diagnosed: find what
  it caught and fix that. The anchor-claims test failed twice on database connection churn while the
  loop guard was given an exemption instead (ADR 0015 §24.13); the exemption hid the real cause.
- Activities heartbeat from the event loop (`_ticking`): CPU-bound work inside an activity (rendering
  a slice, layout, schema validation of a large document) runs in a worker thread
  (`asyncio.to_thread`), never on the loop. A loop blocked longer than the heartbeat timeout makes
  Temporal time out a LIVE attempt, and every retry repeats it (ADR 0015 §23). A thread cannot be
  cancelled: what runs in one must be pure and write nothing (no DB, S3, files); writes stay on the
  loop. Work that must be killable or memory-limited (the report PDF) runs in a child process.
- Database pools are FIXED (`max_overflow=0`) and warmed at startup (`edisc_db.session.warm_pool`):
  a new asyncpg connection runs its SCRAM handshake on the loop. A warmed pool opening a connection
  for a new slot is churn and fails the test (`loopblock.report_product`; ADR 0015 §24.13).
- Every test fails if the event loop was blocked longer than `EDISC_TEST_LOOP_BLOCK_MS` (250 ms;
  `tests/conftest.py`, `edisc_core.loopguard`), in the test process or in any worker it spawned, with
  the blocked stack and a profile of the whole stall in the failure and in the failure artifacts
  (`loop-blocks.json`): sampled every ~10 ms while the loop is overdue; trust its hottest frames,
  not the one sampled stack (ADR 0015 §24.24). Every function the product runs in a thread is listed in
  `OFF_LOOP` there (a call on a loop thread fails the test, however fast): a new `to_thread` site
  adds its function there and a mutation entry (ADR 0015 §24). Async generators over sources that
  may not suspend (`jsonstream`, the RSMF envelope) `await asyncio.sleep(0)` per chunk.

## Measurements (re-run before and after performance changes; results go in docs/runs/)
- `scripts/measure_breakdown.py` (storage per component + per-stage throughput), `scripts/bench_pipeline.py`,
  `scripts/measure_audit_burst.py` (audited reads), `scripts/resume_soak.py` (SIGKILL soak; 50k is the CI test),
  `scripts/measure_new_tenant.py` (a new tenant's first collection on statistics that do not know it).
  Run them on a FRESH test stack (`make test-env-up`); the laptop is noisy, so compare paired runs.

## Mutation checks (`scripts/mutation/`, README there)
- Every new protection gets a catalog entry (one exact edit + the test that must fail); run
  `uv run python scripts/mutation/run.py --kind unit` (and `--kind integration` on the test stack).
  `tests/unit/test_mutation_catalog.py` fails when an entry no longer applies: update it with the refactor.

## Working agreement
- One milestone at a time: implement → tests → run → commit (message ends with the attribution trailer).
- Push after every commit that passes the tests (`git push origin main`); never push a failing commit.
- Never chain `git commit` after checks with `;`: use `make check && git commit ...`. The pre-commit hook is a
  backstop, not a replacement; never bypass it with `--no-verify`.
- No scope creep: later-phase ideas go in `docs/BACKLOG.md`.
- Ask before deviating from a principle. Keep this file, ARCHITECTURE.md and ADRs current when decisions change.
- Start of a session: read docs/HANDOFF.md (state, open decisions, next milestone, gotchas). The next milestone's
  plan: `docs/plans/m18.md` (proposed; its §11 decisions come first).
- Stage explicit paths only (never `git add -A`); `apps/web` belongs to the frontend branch
  (`feat/web-ui`) and is never committed, modified or run from a backend session. Other sessions may share
  this tree: check `git status` first, and never restore files with `git checkout`.
