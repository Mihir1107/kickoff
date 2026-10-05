# Handoff (2026-10-05, end of session)

Read with `CLAUDE.md` (rules), `docs/ARCHITECTURE.md`, `docs/adr/`, `docs/plans/phase-2.md` and `docs/BACKLOG.md`.

## Current state
**Branch and CI:** `main` is green on GitHub CI (lint, typecheck, unit, integration including the 50k SIGKILL
acceptance run). Repo: `github.com/Mihir1107/kickoff`. Every milestone is a separate commit; `git log` is the
history.

**Phase 2 in progress:** M14 (Slack exports) done; M15 (RSMF renders) in progress, next task = step 5
part C (see "In progress: M15"). M16 (report) and M17 (sessions, ADR 0016) follow.

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

**Migrations at head:** 0028.

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

## In progress: M15, RSMF renderer (ADR 0015, accepted; steps 1-4 approved; step 5 parts A and B done)
Read `docs/adr/0015-rsmf-renderer.md` in full (§9-§18 are the decisions and implementation notes, in
order) and `docs/adr/0017-render-worker-versions.md`.

### Status
- **Steps 1-3** (vendored schema, pure renderer, loader + storage): approved 2026-10-04.
- **Step 4** (RenderWorkflow, the render's own custody stream with bounded `render_files_batch` events,
  the renders API, render packages for `edisc-verify`): approved 2026-10-05 (§14).
- **Step 4 follow-ups, approved:** visible stuck sealing, version-keyed render queues
  `renders.r<renderer>.u<unicode>.tz<tzdata>` (§15); render episodes `unroutable` / `sealing_stuck`
  with one alert per episode and history (§16, migration 0028).
- **Step 5:** part A (synthetic corpus, dummy connector 0.3.0) and part B (full crash matrix) done
  (§17, §18). **Part C (render package download endpoint) is NOT started**: it is the next task, spec
  below.
- Renderer **1.2.0**; goldens under `tests/golden/rsmf/1.2.0_unicode-15.0.0_tzdata-2026e_dummy-0.3.0/`
  and `tests/golden/rsmf-corpus/<same key>/`. Migrations at head: **0028**.

### Commits (newest last; all on `main`, pushed)
- `3e47bec` fix: ABA in the batch checkpoint guard, fenced on `(cursor, pages_done)` (ADR 0006).
- `fab31ac` step 4: RenderWorkflow, render custody stream, renders API, render packages.
- `96661bf` stuck sealing made visible; version-keyed render queues.
- `2e9ab29` render episodes (unroutable detection, stuck-sealing history); ADR 0017 draft.
- `73bf727` step 5 A and B: corpus, dummy 0.3.0, crash matrix.
- the commit after `73bf727` (see `git log`): the 2026-10-05 review round (renderer 1.2.0: reactions
  before deletion as history; legacy export layout case; real SIGKILLs in the seal via a test-only
  barrier; routing check once per queue; ADR 0017 accepted; push-after-commit rule; this handoff).

### Decisions taken in the 2026-10-05 reviews (all recorded in ADRs)
1. Package zip: extend our own deterministic STORED zip writer with ZIP64. Do NOT record CRC32 at
   production write time; compute CRC32 while streaming and write it in a data descriptor for every
   entry (one rule for all renders, old and new). (§18.5)
2. Deleted messages keep the reactions recorded before deletion, rendered only as history
   (`edisc.reactions_before_deletion`), never as RSMF `reactions`. (§18.1)
3. `reply_broadcast` renders as a message (it always did); an older-export-layout corpus case covers it.
4. Seal crash coverage uses real SIGKILLs at a test-only barrier, not simulated crashes. (§18.3)
5. The routing check calls DescribeTaskQueue once per queue per run. (§18.4)
6. ADR 0017 accepted: worker image per triple kept while any production made with it is retained plus
   one year, never deleted while a matter under legal hold has productions from it; image digest in
   `render_started`; admission requires the oracle corpus (and goldens) to pass inside the image;
   reproductions store only hashes, the result and their custody stream; manual runbook for old
   workers in v1 (automation in BACKLOG); the API accepts renders even with no current worker
   (unroutable flags it); unknown or retired triples get 409 `renderer_unavailable`. Not implemented.
7. Push after every commit that passes the tests (CLAUDE.md).
8. Earlier (step 4 review): bounded `render_files_batch` events with Merkle roots; dedup on (job,
   options hash, renderer, Unicode, tzdata versions), failed/refused do not count; `export.read` for
   matter managers and tenant admins only; productions never served by `/v1/evidence/{id}/content`;
   render events and anchor rows carry the render id, retention render -> job -> matter.

### Next: step 5 part C, the render package download endpoint (spec agreed, build in a new session)
- `GET /v1/renders/{id}/package?outputs=reference|embed`, permission `export.read` (matter managers,
  tenant admins). Reviewers and auditors get 403. A render that is not sealed gets 409. `reference` is
  the default (outputs listed by hash; the expert passes them to `edisc-verify --file`).
- **Manifest from what was recorded and verified at seal:** the render's events, anchors (listed from
  S3 versions), `render_files` records (SHA-256, size, VersionId) and the job seal anchor. Do NOT read
  every object twice to build it.
- **Audit first:** `audit.render_package_read` (render, mode, manifest SHA-256, actor, request id) is
  committed AND anchored before any byte is sent.
- **One streaming pass:** stream the zip once; every object (output files when embedded, anchors, the
  seal) is read by pinned VersionId and its hash verified against the manifest as it passes. On a
  mismatch: abort the stream, record an abort audit event (`audit.render_package_aborted`) and raise
  an alert.
- **Deterministic zip:** our own writer (extend the renderer's STORED zip code, or a shared pure module
  in `edisc_custody`), STORED entries only, fixed timestamps (1980-01-01), fixed entry order, fixed
  permissions, no extra fields beyond ZIP64 where needed, ZIP64 when sizes or entry counts need it,
  CRC32 computed while streaming and written in a data descriptor for EVERY entry. Two downloads of the
  same render must be byte-identical (test it). `exported_at` must not make the manifest differ between
  downloads (use the seal time, or omit it).
- `edisc-verify` must accept the zip directly as well as a directory.
- **Compatibility tests:** the zip opens and verifies with Python `zipfile`, Info-ZIP `unzip`, `7z`,
  and macOS Archive Utility (`ditto -x -k` is a scriptable stand-in; check by hand once).
  ZIP64: more than 65,535 entries generated locally; more than 4 GiB via a synthetic stream into a
  hashing sink (no 4 GiB file on disk), then read back with our streaming reader
  (`edisc_custody.archive`) and `zipfile` over a seekable synthetic source if feasible.
- Share one generator between the directory exporter (`export_render_package`) and the zip stream.
- Then the rest of M15: §11 (oversized attachments as external natives, required before production).

### Open questions / waiting on the user
1. Relativity licence (validator in CI) - see "Open decisions" above.
2. Real Slack exports for the corpus: drop each as `tests/fixtures/slack_exports/<name>/export.zip`
   with a hand-reviewed `expected.json`; `tests/integration/corpus/cases.py::real_exports` lists them.
   Also confirm every *(confirm on real export)* item in ADR 0014.
3. ADR 0017 implementation order (registry file, image digest in custody, reproductions, runbook) is
   not scheduled.
4. No detection yet for a render waiting on a queue that never had pollers *before* the unroutable
   threshold (300 s); by design it is flagged after the threshold.

### How to run what matters here
- `make check` (lint, typecheck, unit). Integration only on the ephemeral stack:
  `MIN_FREE_GB=12 make test-env-up`, then `make test-integration-only TESTS=...`, then
  `make test-env-down` (this laptop often has 14-16 GB free; the stack uses under 1 GB).
- Render suites: `tests/integration/renders` (workflow, operations, crash matrix with SIGKILLs),
  `tests/integration/corpus` + `tests/integration/api/test_corpus_export.py` (corpus),
  `tests/integration/acceptance/test_render_batch_boundary.py` (500/501, about 2 minutes),
  `tests/integration/api/test_renders.py`, `tests/integration/custody/test_render_package.py`.
- Goldens: `EDISC_RECORD_RSMF=1` (renderer) and `EDISC_RECORD_CORPUS=1` (corpus) only after a
  deliberate renderer or dummy version bump; never in CI; never overwrite.
- Mutation-check every new protection: `cp` the file aside, break it, run the test, `cp` it back
  (never `git checkout`).

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
