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
- `infra/docker-compose.yml`: Postgres, Redis, MinIO (object lock), Temporal + UI, Elasticsearch. Pinned by tag + digest.
  MinIO is the `pgsty/minio` community rebuild (upstream stopped publishing images). Temporal UI: http://localhost:8080.
- `tests/unit` (no services), `tests/integration` (real compose services, **never mock DB, S3 or Temporal**), `tests/golden`.

## Commands
```
make sync               # uv sync --all-packages
make hooks              # once per clone: pre-commit runs ruff lint, ruff format --check, mypy on staged .py
make up / down          # infra up (waits for health, runs idempotent init jobs) / stop
make up-ci              # subset CI uses (no UI, no Elasticsearch)
make nuke               # down + delete volumes; refuses unless EDISC_ENV=local|test|ci
make migrate            # bootstrap roles/schema as superuser (idempotent), then alembic upgrade head as owner
uv run python -m edisc_db.migrate downgrade <rev>   # explicit target only; never 'base' outside scratch DBs
make lint fmt typecheck # ruff, ruff format, mypy --strict
make test               # unit tests
make test-integration   # FRESH ephemeral stack (-p edisc-test, .env.test, other ports), tests, then down -v
make test-env-up / test-integration-only / test-env-down   # keep the test stack up while iterating
                        # up/up-ci/test targets refuse below MIN_FREE_GB (15) free disk
make worker / api       # Temporal worker (+ maintenance queue and sweeper schedules) / API
```

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
  only when `scripts/temporal_patch_check.py` exits 0.
- Per-batch writes (items + job_items + custody event + checkpoint + counts) happen in ONE transaction
  (`edisc_worker.pipeline.Pipeline.process_batch`, ADR 0006): evidence and file downloads happen BEFORE it, the
  transaction starts with the checkpoint guard (moved cursor = no-op), links are inserted before the custody event
  (deferred FK) so the Merkle root covers exactly the new links. `in_scope` lives on `job_items`, not on items.
  Absence detection only for clean units against earlier clean collections of the same unit (ADR 0005).
- Schema changes: new Alembic revision in `packages/db/migrations/versions/` (hand-written SQL, run via `split_sql`),
  update `edisc_db.models` to match (drift test fails otherwise), add RLS + grants for any new tenant table.
- Tenant isolation: FORCE RLS on every tenant table. The app connects as `edisc_app` (not owner, not superuser,
  no BYPASSRLS) and sets `SET LOCAL app.tenant_id` per transaction via the single `tenant_tx()` helper, in activities too.
  Superuser is only for migrations and tamper tests.
- Evidence (`edisc_evidence.writer.EvidenceWriter`, ADR 0002): pages -> job-scoped keys, single pass; files -> staging
  bucket (hash while streaming) -> server-side copy into content-addressed `t/{tenant}/files/sha256/...` (dedup per
  tenant, advisory-locked, destination verified). Evidence hash = our own streaming SHA-256, never S3's composite.
  Lock set at create; abort multipart on any failure; `recover_pending` at job finalize. No evidence on local disk.
- Retention: rolling window `min(matter.retention_until, now + EDISC_EVIDENCE_RETENTION_WINDOW_DAYS)`, extended while the
  matter is active (extension job: backlog, required before production). Never lock for the full matter upfront.
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
  `anchor_if_due` (WORM anchor of the head). Lifecycle events and every N events are anchored; `seal_job_chain` at
  finalize; `sweep_anchors` (periodic) seals overdue/abandoned streams. `verify_chain` (DB) and `edisc-verify` (offline package, ADR 0008) share `ChainVerifier`. Anchors are always
  listed from S3 versions, never from the DB. Merkle = RFC 6962, leaves ordered by idempotency_key.
- Keep `edisc_custody.package`/`cli`/`chain`/`merkle` free of DB and cloud imports (a test enforces it).
- `completed_unverified` is never presented as a clean completion (ADR 0005).
- Secrets: `.env.example` has placeholders only. Never commit `.env`. Wrap secrets in `SecretStr`.
- Dummy connector (`edisc_connector_dummy`): the golden dataset and its own oracle. Expected values in tests come
  from `Dataset`, never from collected data. Output is byte-identical per (spec, epoch); changing it requires a
  `DummyConnector.version` bump and regenerating `tests/golden/dummy/small.json`.
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

## Working agreement
- One milestone at a time: implement → tests → run → commit (message ends with the attribution trailer).
- Never chain `git commit` after checks with `;`: use `make check && git commit ...`. The pre-commit hook is a
  backstop, not a replacement; never bypass it with `--no-verify`.
- No scope creep: later-phase ideas go in `docs/BACKLOG.md`.
- Ask before deviating from a principle. Keep this file, ARCHITECTURE.md and ADRs current when decisions change.
