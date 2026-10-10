# CLAUDE.md: project rules for future sessions

eDiscovery collaboration-data **collection module** (client > matter > workspace, Relativity model).
It collects Slack/Teams/etc. communications defensibly and emits RSMF + native JSON + HTML + a collection report.
The plan of record is in `docs/ARCHITECTURE.md` and `docs/adr/`. Read them before changing the core.

## Non-negotiable principles (these override convenience)
1. **No silent data loss.** Reconcile expected vs collected per unit. Gaps fail loudly
   (`completed_with_gaps` / `completed_unverified` / `failed`, never `completed`). Never swallow
   exceptions in the collection path: no bare `except`, no `except Exception: pass`, no logging-and-continuing.
2. **Tamper evidence.** Every raw item is SHA-256 hashed at collection and stored in WORM (S3 Object
   Lock, COMPLIANCE mode). Application code never modifies or deletes raw data.
3. **Chain of custody.** Every action (connect, collect, write, render, export) appends a custody event
   to a hash chain: `event_hash = sha256(prev_hash + canonical_json(event))`. `custody_events` is append-only (DB triggers).
4. **Idempotency.** Key = `tenant_id + source + source_item_id + content_hash`. An edit is a new version, never an overwrite.
5. **Resumability.** Any job can be SIGKILLed anywhere and resumes from its last DB checkpoint with zero gaps and zero duplicates.
6. **Tokens stay with us.** No third-party integration platforms or hosted auth brokers. Tokens are
   envelope-encrypted per tenant, never logged, and never passed through Temporal payloads.
7. **Thin connectors.** Connectors authenticate, enumerate and fetch raw only. All interpretation goes in `edisc-normalizer`.
8. **Streaming, never loading.** Bounded chunks. Hash while uploading. Never hold a whole conversation/collection in memory.

## Layout
- `packages/*`: libraries (`edisc_core`, `edisc_db`, `edisc_evidence`, `edisc_custody`, `edisc_connectors_base`,
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
make worker / api       # Temporal worker (+ maintenance queue, sweeper schedules, exports, renders) / API
```

## API (M13, ADR 0013)
- `apps/api` (`edisc_api`): tenant from the Host subdomain (`tenant_id_for_subdomain`) + the token's IdP
  (`tenant_idps`) + an active principal. NEVER read a tenant id from a body, query or header. Unknown
  tenants and all auth failures get the same 401.
- Every `/v1` route declares `openapi_extra=perm(...)` and calls `authorize(s, caller, P.X, Scope(...))`
  inside its `tenant_tx` (404 if the caller cannot see the target, 403 if it can but lacks the permission).
  Request models `extra="forbid"`; response models only (no ORM rows or dicts); cursor pagination
  (`edisc_api.pagination`) on every list.
- State changes: job actions are custody events in the job stream; everything else is
  `edisc_api.audit.record` (custody stream = tenant id) with `caller.actor` and the request id. Evidence
  content reads are audited BEFORE bytes are returned.
- Credentials only through `edisc_db.connection_tokens`; the API test suite scans every response for
  every credential it handed in.
- Nothing is deleted: memberships and role assignments are ended (`removed_at` / `revoked_at`).

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

## Collection report and preview (M16, ADR 0018; steps 1, 2 and 4 built: model, HTML, loader, stream)
- Read ADR 0018 in full (and its §19/§20 implementation notes) before changing M16 code;
  `docs/HANDOFF.md` has what is next. The report has its own custody stream (a sealed job chain is
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
- PDF bytes are promised only inside the pinned linux/amd64 report image (`spikes/m16-pdf/` is the
  prototype: vendored fonts by SHA-256, fontconfig isolated, Debian snapshot, TrueType CJK fonts).
  PDF goldens are authoritative only in CI on amd64; locally they run in the image or skip with a
  visible reason, never silently.

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
- Schema changes: new Alembic revision in `packages/db/migrations/versions/` (hand-written SQL, run via `split_sql`),
  update `edisc_db.models` to match (drift test fails otherwise), add RLS + grants for any new tenant table.
- Tenant isolation: FORCE RLS on every tenant table. The app connects as `edisc_app` (not owner, not superuser,
  no BYPASSRLS) and sets `SET LOCAL app.tenant_id` per transaction via the single `tenant_tx()` helper, in activities too.
  Superuser is only for migrations and tamper tests.
- Evidence (`edisc_evidence.writer.EvidenceWriter`, ADR 0002 + amendment): pages -> job-scoped keys, single pass.
  Files -> content-addressed `t/{tenant}/files/sha256/...` (dedup per tenant): a key already `complete` in the registry
  is a dedup hit (no lock, no upload); small files (<= `EDISC_EVIDENCE_SMALL_FILE_MAX_BYTES`) are hashed in memory and
  PUT once (If-None-Match, lock, ChecksumSHA256) under the advisory lock; large files stream to the staging bucket and
  are server-side copied. Source hash persisted before any WORM write. A page's files are written with bounded
  concurrency (`EDISC_EVIDENCE_FILE_CONCURRENCY`). Evidence hash = our own SHA-256, never S3's composite.
  Lock set at create; abort multipart on any failure; `recover_pending` at job finalize. No evidence on local disk.
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
- Keep `edisc_custody.package`/`cli`/`chain`/`merkle` free of DB and cloud imports (a test enforces it).
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
- Activities heartbeat from the event loop (`_ticking`): CPU-bound work inside an activity (rendering
  a slice, layout, schema validation of a large document) runs in a worker thread
  (`asyncio.to_thread`), never on the loop. A loop blocked longer than the heartbeat timeout makes
  Temporal time out a LIVE attempt, and every retry repeats it (ADR 0015 §23). A thread cannot be
  cancelled: what runs in one must be pure and write nothing (no DB, S3, files); writes stay on the
  loop. Work that must be killable or memory-limited (the report PDF) runs in a child process.
- Every test fails if the event loop was blocked longer than `EDISC_TEST_LOOP_BLOCK_MS` (250 ms;
  `tests/conftest.py`, `edisc_core.loopguard`), in the test process or in any worker it spawned, with
  the blocked stack in the failure. Every function the product runs in a thread is listed in
  `OFF_LOOP` there (a call on a loop thread fails the test, however fast): a new `to_thread` site
  adds its function there and a mutation entry (ADR 0015 §24). Async generators over sources that
  may not suspend (`jsonstream`, the RSMF envelope) `await asyncio.sleep(0)` per chunk.

## Measurements (re-run before and after performance changes; results go in docs/runs/)
- `scripts/measure_breakdown.py` (storage per component + per-stage throughput), `scripts/bench_pipeline.py`,
  `scripts/measure_audit_burst.py` (audited reads), `scripts/resume_soak.py` (SIGKILL soak; 50k is the CI test).
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
- Start of a session: read docs/HANDOFF.md (state, open decisions, next milestone, gotchas).
- Stage explicit paths only (never `git add -A`); `apps/web` belongs to the frontend branch
  (`feat/web-ui`) and is never committed, modified or run from a backend session. Other sessions may share
  this tree: check `git status` first, and never restore files with `git checkout`.
