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
- `packages/*`: libraries (`edisc_core`, `edisc_evidence`, `edisc_custody`, `edisc_connectors_base`,
  `edisc_connector_*`, `edisc_normalizer`, `edisc_renderers`). src layout, one uv workspace member each.
- `apps/api` (`edisc_api`): FastAPI. `workers/collection` (`edisc_worker`): Temporal workers, one task queue per source.
- `infra/docker-compose.yml`: Postgres, Redis, MinIO (object lock), Temporal + UI, Elasticsearch. All image versions pinned.
- `tests/unit` (no services), `tests/integration` (real compose services, **never mock DB, S3 or Temporal**), `tests/golden`.

## Commands
```
make sync               # uv sync --all-packages
make up / down          # infra up (waits for health) / stop
make nuke               # down + delete volumes (only way to discard local WORM data)
make migrate            # alembic upgrade head
make lint fmt typecheck # ruff, ruff format, mypy --strict
make test               # unit tests
make test-integration   # integration tests (needs `make up`)
make worker / api       # run the Temporal worker / API
```

## Conventions
- Python 3.12, `uv` only (no pip). Add deps with `uv add --package <member> <dep>`.
- mypy `--strict` on all source (packages/apps/workers). ruff is the formatter and linter.
- All timestamps are timezone-aware UTC (`edisc_core.time`). Naive datetimes are bugs (ruff DTZ enforces this).
- IDs are UUIDv7 (`edisc_core.ids`).
- Hash inputs use RFC 8785 canonical JSON (`edisc_core.canonical`), never `json.dumps`.
- Workflows carry IDs and small cursors only. Raw data, tokens and large payloads never enter Temporal history.
- The DB checkpoint is the source of truth for resume, not Temporal heartbeat details.
- Per-batch writes (items + job_items + custody event + checkpoint + counts) happen in ONE transaction.
- The app connects as the least-privilege `edisc_app` role. Superuser is only for migrations and tamper tests.
- Secrets: `.env.example` has placeholders only. Never commit `.env`. Wrap secrets in `SecretStr`.

## Working agreement
- One milestone at a time: implement → tests → run → commit (message ends with the attribution trailer).
- No scope creep: later-phase ideas go in `docs/BACKLOG.md`.
- Ask before deviating from a principle. Keep this file, ARCHITECTURE.md and ADRs current when decisions change.
