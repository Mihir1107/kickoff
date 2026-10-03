# Handoff (2026-10-02)

Read with `CLAUDE.md` (rules), `docs/ARCHITECTURE.md`, `docs/adr/`, `docs/plans/phase-2.md` and `docs/BACKLOG.md`.

## Current state
**Branch and CI:** `main` is green on GitHub CI (lint, typecheck, unit, integration including the 50k SIGKILL
acceptance run). Repo: `github.com/Mihir1107/kickoff`. Every milestone is a separate commit; `git log` is the
history.

**Phase 1 is complete (M0–M13):**
- **Evidence:** WORM via S3 Object Lock COMPLIANCE on MinIO, with a rolling retention window plus an
  extension floor.
- **Custody:** hash-chained custody with WORM anchors (coalesced claims), seals, and the offline verifier
  `edisc-verify`.
- **Resilience:** RLS tenant isolation, envelope-encrypted tokens and a Redis rate limiter.
- **Collection:** the dummy connector plus Slack normalizer, and an exactly-once batch pipeline with
  reconciliation.
- **Orchestration:** Temporal workflows (continue-as-new, error classes, cancel, auth pause/resume,
  sweeper schedules).
- **API (M13):** tenant from the Host header + IdP + principal; scoped roles; client-owned connections;
  multi-scope jobs; `Idempotency-Key`; audited evidence reads; trusted-proxy client IP; auth-failure
  throttling.

**Since the last plan review:**
- storage/throughput options 1 and 2;
- the retention extension floor;
- `X-Forwarded-For` only from trusted proxies;
- the audit-burst measurement;
- the anchor-storm fix (migration 0017).

**Migrations at head:** 0022.

**Not done in Phase 1:** the 1M-message soak. Laptop disk is too small (~15 GB free; it needs ~21 GB). It is
in the backlog for a cloud VM: `scripts/resume_soak.py --messages 1000000 --kills 10`.

## Decisions (all recorded)
- **ADR 0013 (accepted):** connections are owned by the client, and workspaces are modelled now. Both are
  *pending product-owner confirmation*. Fixed roles; content reads audited.
- **Phase 2:** all eight decisions are recorded in `docs/plans/phase-2.md` ("Decisions (2026-10-02)").

## Open decisions / waiting on the user
1. **Relativity licence:** does the organisation hold one? Only then may the proprietary RSMF validator SDK
   run in CI (in a private .NET container, never redistributed). The RSMF JSON schema is BSD-3 and can be
   vendored now. Licence terms are in phase-2.md, decision 4.
2. **Real Slack exports:** the user is providing a Developer Program sandbox export and a free-plan workspace
   export. Make both fixtures when they arrive. Until then build against the format docs and the dummy.
3. Product-owner confirmation of ADR 0013 decisions (a) and (c).
4. A cloud VM for the 1M soak.

## Current milestone: M14, Slack export ingestion (ADR 0014, accepted with R1-R7)
Done:
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

Next, in order:
1. **Real-export fixtures** when the user sends them (Developer Program sandbox, free-plan workspace):
   confirm every *(confirm on real export)* item in ADR 0014 and measure range reads on them.
2. **M15** RSMF renderer (Relativity licence still to confirm).
3. Backlog for a cloud VM: the 5 GB streaming archive run (ADR 0014 section 3) with the 1M soak.

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
- `timedelta(0)` is falsy: use `is not None` checks for optional durations (a real bug, fixed).
- **Anchoring:** never write an anchor without winning the claim. One anchor per due point.

**Testing:**
- **Mutation checks:** after writing a test, break the code it protects and confirm the test fails. Several
  tests were vacuous until this was done (see commits).
- **Noisy laptop:** absolute timings vary up to 2x between runs. Use paired measurements (the scripts in
  CLAUDE.md, "Measurements").
- **API tests:** they scan every response for every credential they sent. Use `secret()` from
  `tests/integration/api/conftest.py` for any credential you pass.
- **Dev IdP:** `EDISC_API_DEV_IDP` exists only for local/test/ci. Its key is cached; re-parsing it per request
  cost about 75 ms and once masked a measurement.
