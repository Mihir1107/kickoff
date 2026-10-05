# Handoff (2026-10-05, end of session: M15 done incl. the §21 review; next: M16)

This file alone is enough to start M16. Rules are in `CLAUDE.md` (read it first, in full); the plan of
record is `docs/ARCHITECTURE.md`, `docs/adr/`, `docs/plans/phase-2.md` and `docs/BACKLOG.md`.

## Current state
**Branch and CI:** `main`, green on GitHub CI (lint, typecheck, unit, integration including the 50k SIGKILL
acceptance run). Repo: `github.com/Mihir1107/kickoff`. One commit per milestone or review round;
`git log` is the history. Migrations at head: **0029**. Renderer **1.3.1**, dummy connector **0.4.0**.

**Phase 1 (M0-M13) is complete:** WORM evidence (S3 Object Lock COMPLIANCE on MinIO, rolling retention
plus extension floor); hash-chained custody with WORM anchors, seals and the offline verifier
`edisc-verify`; RLS tenant isolation, envelope-encrypted tokens, a Redis rate limiter; the dummy
connector, the Slack normalizer and an exactly-once batch pipeline with reconciliation; Temporal
workflows; the API (tenant from Host + IdP + principal, scoped roles, audited evidence reads). Not done:
the 1M-message soak (needs a cloud VM; `scripts/resume_soak.py --messages 1000000 --kills 10`).

**Phase 2:** M14 (Slack exports) done; **M15 (RSMF renders) done** (below); **next: M16**; then M17
(UI, sessions: ADR 0016 and the M17 backend plan in `docs/plans/phase-2.md`).

## Next: M16, HTML preview and the collection report
Scope (`docs/plans/phase-2.md`, "M16", and decision 6 there):
1. **Preview:** a conversation-day view from the same intermediate as RSMF (the render loader's
   `SliceInput`s: threads, edits, deletions, reactions, attachments; deleted messages' reactions only
   as history, ADR 0015 §18.1). Static, sanitised HTML: everything escaped, no scripts, no external
   resources, strict CSP. Attachments link to the audited content endpoint (purpose `preview`).
   Reviewers get previews (phase-2 decision 5); RSMF stays `export.create` / `export.read`.
2. **Collection report** in HTML, JSON and PDF (PDF REQUIRED, decision 6: rendered from the same HTML
   template with fixed metadata, byte-reproducible), deterministic and hashed, recorded as the
   lifecycle custody event `report_generated` with the report's hash. Contents: scopes (ranges,
   policies), access tier, plan and granted scopes; blind spots; counts per unit; gaps, unverifiable
   and failed units with reasons; file unavailability; access-lost and no-longer-observed events;
   pauses (who re-authorized, how long); connector and normalizer versions; the actor of every action;
   the custody verification result (events, batches, items, anchors, Merkle roots, seal); the evidence
   store's lock settings; retention gaps (ADR 0002); for exports the verbatim `ARCHIVE_CAVEAT`
   (ADR 0014); renders and their external natives (ADR 0015 §11 says the report lists them).
3. **Rules:** `completed_unverified`, `completed_with_gaps`, `completed_with_failed_units` and
   `completed_against_archive` are never presented as success (`JobStatus.is_clean`; the API's
   `clean` / `clean_basis` / `caveat` fields already follow this). A report is evidence of a job: it
   is written like a production (WORM, pinned version, our SHA-256), audited when read, anchored
   before the first byte (CLAUDE.md: every route that returns evidence bytes).

Where the data is (verified 2026-10-05):
- Jobs, units, statuses: `collection_jobs`, `work_units` (`recon_status`: `ReconStatus` in
  `edisc_core.schemas`), `work_unit_scopes`, `collection_scopes`; job pauses: `job_pauses`
  (`paused_at`, `resumed_at`, `reason`); alerts: `alerts`; retention lapses: `retention_gaps`.
- Events per item: `items` of `item_type = event` with `EventKind` (`file_unavailable`,
  `no_longer_observed`, `access_lost`, `access_restored`, ...), linked to the job by `job_items`.
- Custody: `verify_chain` (`edisc_custody.log`) gives the verification result; the job package
  (`edisc_custody.export`) is what an expert checks offline.
- Plan tier and granted scopes: columns on `connections`. **Blind spots are NOT stored in a column**:
  they are in the `audit.connection_created` payload (tenant audit stream, written by
  `apps/api/src/edisc_api/routes/connections.py`) and, for exports, in `slack_exports.findings`
  (`blind_spots`). Decide in the M16 ADR where the report reads them from (a recorded source the
  verifier can check, not a live re-validation).
- Renders: `renders`, `render_files`, `render_natives` (ADR 0015 §14, §20-§22).

Suggested order (each step: implement, tests, mutation entries in `scripts/mutation/catalog.py`, run,
commit, push): (1) **ADR 0018 first, for review:** the report's data model and sources (above), the
deterministic HTML template, the PDF toolchain and how its bytes are pinned (choose and pin the
engine like tzdata/Unicode for RSMF: a version in the golden key), storage and custody
(`report_generated`), the preview's sanitiser and CSP, permissions; (2) the report JSON (pure, from
a loader, oracle-tested against the dummy dataset like the renderer); (3) HTML; (4) PDF; (5) storage,
custody event, API with first-byte audit tests; (6) preview. Reuse: the render loader for the
preview, `ZipSizer`/`zipwriter` if a bundle is needed, `first_byte.py` for content routes.

## Open decisions / waiting on the user
1. **RSMF consumer limits (ADR 0015 §22.4, new):** Relativity documents that `rsmf.zip` "must ... use
   DEFLATE compression" (we write STORED, for byte identity) and that "RSMF files greater than 2 GB are
   not supported" (our structural rule allows a part's zip up to about 4 GiB, about 5.4 GB of `.rsmf`
   after base64). Decide (a) a 2 GB `.rsmf` limit as the structural rule, (b) DEFLATE with a pinned
   implementation, or confirmation from Relativity that STORED is accepted. Both are renderer bumps.
2. **Relativity licence:** only with one may the proprietary RSMF validator SDK run in CI (private
   .NET container, never redistributed); terms in phase-2.md, decision 4.
3. **Real Slack exports:** a Developer Program sandbox export and a free-plan workspace export, to
   become fixtures (`tests/fixtures/slack_exports/<name>/export.zip` + a hand-reviewed
   `expected.json`; `tests/integration/corpus/cases.py::real_exports`). Confirm every *(confirm on real
   export)* item in ADR 0014 then.
4. Product-owner confirmation of ADR 0013 decisions (a) and (c).
5. A cloud VM: the 1M soak, a 5 GB export ingestion, a real multi-GB native (BACKLOG).
6. ADR 0017 (worker image per renderer triple) is accepted but not scheduled.

## Decisions (all recorded)
- ADR 0013 (accepted): client-owned connections, workspaces modelled now, fixed roles, audited reads.
- Phase 2: the eight decisions in `docs/plans/phase-2.md` ("Decisions (2026-10-02)").
- ADR 0015 §9-§22: every RSMF decision and implementation note, in order (§18 and §19.14 are the
  2026-10-05 review decisions; §22 the review of the natives build).

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
- **Mutation checks:** after writing a test, break the code it protects and confirm the test fails, through
  `scripts/mutation/` (it isolates the bytecode cache per run: two same-sized edits of one file within a
  second once loaded a stale `.pyc` and made a break look harmless). Several tests were vacuous until this
  was done.
- **Noisy laptop:** absolute timings vary up to 2x between runs. Use paired measurements (the scripts in
  CLAUDE.md, "Measurements").
- **API tests:** they scan every response for every credential they sent. Use `secret()` from
  `tests/integration/api/conftest.py` for any credential you pass.
- **Dev IdP:** `EDISC_API_DEV_IDP` exists only for local/test/ci. Its key is cached; re-parsing it per request
  cost about 75 ms and once masked a measurement.
