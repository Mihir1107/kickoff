# Handoff (2026-10-06, end of session: M16 designed (ADR 0018 accepted and amended, PDF spike S1 passed); next: build M16)

This file alone is enough to build M16. Rules are in `CLAUDE.md` (read it first, in full). The M16
design of record is **`docs/adr/0018-collection-report-and-preview.md`** (read it in full before any
code); the rest of the plan of record is `docs/ARCHITECTURE.md`, `docs/adr/`, `docs/plans/phase-2.md`
and `docs/BACKLOG.md`. `docs/plans/m16.md` is the superseded proposal (history only).

## Current state
**Branch and CI:** `main`, green on GitHub CI (lint, typecheck, unit, integration including the 50k SIGKILL
acceptance run). Repo: `github.com/Mihir1107/kickoff`. One commit per milestone or review round;
`git log` is the history. Migrations at head: **0029**. Renderer **1.3.1**, dummy connector **0.4.0**.
Nothing of M16 is built: only the ADR, the spike (`spikes/m16-pdf/`, the evidence for byte identity)
and its manual-only CI workflow (`.github/workflows/spike-m16-pdf.yml`, `gh workflow run
spike-m16-pdf`).

**Phase 1 (M0-M13) is complete:** WORM evidence (S3 Object Lock COMPLIANCE on MinIO, rolling retention
plus extension floor); hash-chained custody with WORM anchors, seals and the offline verifier
`edisc-verify`; RLS tenant isolation, envelope-encrypted tokens, a Redis rate limiter; the dummy
connector, the Slack normalizer and an exactly-once batch pipeline with reconciliation; Temporal
workflows; the API (tenant from Host + IdP + principal, scoped roles, audited evidence reads). Not done:
the 1M-message soak (needs a cloud VM; `scripts/resume_soak.py --messages 1000000 --kills 10`).

**Phase 2:** M14 (Slack exports) done; M15 (RSMF renders) done; **M16 designed, next to build**; then
M17 (UI, sessions: ADR 0016 and the M17 backend plan in `docs/plans/phase-2.md`).

## Next: build M16
ADR 0018 §16 is the order. Each step: implement → tests → mutation entries in
`scripts/mutation/catalog.py` → run → commit → push.

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

**PDF (spike S1):**
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
