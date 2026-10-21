# eDiscovery collection module

[![ci](https://github.com/Mihir1107/kickoff/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Mihir1107/kickoff/actions/workflows/ci.yml)

This README is the entry point for anyone new to the repository: what it is, how to run, demo,
verify and test it, and where to go next. It links to the detailed documents rather than repeating them.

## Contents
1. [What this is](#what-this-is)
2. [Status](#status)
3. [Prerequisites](#prerequisites)
4. [First-time setup](#first-time-setup)
5. [The demo](#the-demo)
6. [Local UIs and ports](#local-uis-and-ports)
7. [Verifying offline](#verifying-offline)
8. [Tests](#tests)
9. [Architecture in brief](#architecture-in-brief)
10. [Repository map](#repository-map)
11. [Key concepts](#key-concepts)
12. [Decision records](#decision-records)
13. [Rules for contributors](#rules-for-contributors)
14. [Troubleshooting](#troubleshooting)
15. [Open questions and next work](#open-questions-and-next-work)

## What this is

The **collection module** of an eDiscovery platform. It collects workplace communications
(today: Slack export ZIPs) for litigation and investigations, and hands review platforms a
production they can load and an expert can check.

In the EDRM flow (identification, preservation, collection, processing, review, analysis,
production, presentation) it covers **collection** fully, the start of **processing**
(normalizing raw Slack JSON into versioned messages, threads, edits, reactions and files), and the
**production** format for chat (RSMF per conversation per day, which Relativity and similar
review tools load). Identification (choosing custodians and channels) is an API input; review,
analysis and presentation belong to the platform that consumes the output.

Data is organized tenant > client > matter > workspace, as in Relativity.

What it guarantees, and how:

| Guarantee | Mechanism |
|---|---|
| Nothing missed | Every conversation-day is a unit; expected vs collected is reconciled per unit. Any gap makes the job `completed_with_gaps` (or worse), never `completed` |
| Nothing changed | Every raw page, file and export is SHA-256 hashed while streamed and stored in WORM storage (S3 Object Lock, COMPLIANCE mode) |
| History proven | Every action is an event in a hash chain; batches carry Merkle roots; the chain is anchored to WORM regularly and sealed at the end |
| Verifiable offline | `edisc-verify` checks a downloaded package with no database, no object store and no network |

## Status

Built and tested on `main` (each milestone has its ADR or plan; details in [docs/HANDOFF.md](docs/HANDOFF.md)):

| Milestone | What |
|---|---|
| M0-M2 | uv workspace, CI, Docker Compose infra with a WORM bucket, architecture and ADRs 0001-0007 |
| M3 | Core library: settings, UTC time, UUIDv7, RFC 8785 canonical JSON, redacting logs |
| M4 | Postgres schema, roles, FORCE row-level security, append-only triggers |
| M5 | Custody log: hash chain, RFC 6962 batch roots, WORM anchors, offline verifier |
| M6 | Streaming evidence writer to S3 Object Lock, staging promotion, rolling retention |
| M7 | Envelope encryption of connection tokens (KMS semantics, rotation) |
| M8 | Distributed rate limiting (Redis token buckets) |
| M9 | Connector protocol and the deterministic dummy connector (golden dataset + oracle) |
| M10 | Pure Slack normalizer: versions, edits, reactions, files |
| M11 | Collection pipeline: exactly-once batches, reconciliation, absence detection |
| M12 | Temporal workflows: fan-out per unit, resume after SIGKILL, cancellation, replay goldens |
| M13 | API: tenants, principals, scoped roles, jobs, audited reads |
| M14 | Slack export ZIP ingestion: upload, hash, lock, validate, collect |
| M15 | RSMF 2.0 renderer, byte-identical, with its own custody stream and packages |
| M16 | Collection report (JSON, HTML, reproducible PDF/A) and HTML preview, recomputed offline |
| M17 | Browser sessions, CSRF, OpenAPI contract, job streams (SSE), guarded downloads, source installs, access-lost handling (steps 7 and 9 not built, below) |
| M18 steps 0-4 | Least-privilege storage identities, upload quotas, legal hold |

Not built:
- M18 step 5 (standalone verifier package) is **built on branch `m18-step5`, not merged** (below).
- M18 step 6 (destruction workflow) and step 7 (destruction certificate).
- M17 step 7 (ready-made Slack app manifest): waits for legal review of the internal-app token exception.
- M17 step 9 (web UI): on branch `feat/web-ui`, another workstream. The frontend must still move
  to the install routes (`production-readiness` warns on main until it does).
- Live Slack API and Teams connectors (Phase 3; `packages/connectors/slack` and `teams` are stubs).
- Everything cloud: AWS KMS, AWS bucket policies, the report image in a registry, the 1M-message soak.

Blocked, and on what:
- **Open CI failure:** branch `m18-step5`, job `report-pdf`, run
  [39535223933](https://github.com/Mihir1107/kickoff/actions/runs/39535223933): "NOT ADMITTED: no
  PDF goldens for toolchain 1e861ea7e088". Expected: the new verifier package changes the report
  image's toolchain id, so its PDF goldens must be recorded by hand inside the image. That needs 15
  GB free on the recording machine. `main` is green (run
  [39537163545](https://github.com/Mihir1107/kickoff/actions/runs/39537163545)).
- Real IdP and Slack/Microsoft app confirmation: needs developer tenants (ADR 0016, "To confirm on real tenants").
- Real Slack exports as fixtures: every *(confirm on real export)* item in ADR 0014.
- Relativity's own RSMF validator: needs a Relativity licence.

**Scope set by the platform owner: Slack ZIP exports only for now.** Live API collection and
Teams come later.

## Prerequisites

| Tool | Version used for this README | Notes |
|---|---|---|
| Docker Desktop | Docker 29.8.0, Compose v5.5.1, buildx 0.37.0 | Compose must support `up --wait`; buildx builds the report image |
| [uv](https://docs.astral.sh/uv/) | 0.10.11 | Installs the pinned Python itself |
| Python | 3.12.13, pinned in `.python-version` | Pinned to the patch: it fixes the Unicode tables that RSMF bytes depend on |
| Free disk | 15 GB for `make up` and the test targets; 10 GB for the demo | See below |
| OS | macOS 27 on Apple silicon (arm64) here; CI runs ubuntu-24.04 amd64 | |

Why the disk guard: test evidence is locked in COMPLIANCE mode and can only be removed by
destroying the volume, and a full disk turns Docker Desktop's VM read-only. The make targets refuse
below 15 GB (`MIN_FREE_GB`), the demo below 10 GB. Do not lower them; free disk instead.

The report PDF is made only inside a pinned **linux/amd64** image (`deploy/report-image/`). On
Apple silicon it runs under amd64 emulation: correct but slow. The first build downloads a base
image, a Debian snapshot and fonts (several minutes); later builds are cached.

## First-time setup

From a fresh clone (each command was run in a fresh clone for this README):

```
git clone https://github.com/Mihir1107/kickoff.git
cd kickoff
make sync                  # uv sync --all-packages: Python 3.12.13 and every workspace member
make hooks                 # once per clone: pre-commit runs ruff and mypy on staged files
cp .env.example .env       # make also does this when .env is missing
make check                 # lint, mypy --strict, unit tests: no services needed (about 2.5 min)
```

`.env` is never committed. For local use the placeholders in `.env.example` work as they are; they
must be replaced (with real secrets from a secret manager) anywhere that is not a laptop:

| Variables | What |
|---|---|
| `EDISC_PG_*_PASSWORD`, `TEMPORAL_DB_PASSWORD` | Postgres superuser (migrations), schema owner, app role, sweeper role, Temporal's role |
| `EDISC_S3_ROOT_USER`, `EDISC_S3_ROOT_PASSWORD` | MinIO root. Provisioning only (compose `minio` and `minio-init`); no service may use it |
| `EDISC_S3_ACCESS_KEY` / `_SECRET_KEY` | Storage identity `edisc-app`: the API and every worker. No deletes, no hold flags |
| `EDISC_S3_HOLD_ACCESS_KEY` / `_SECRET_KEY` | Identity `edisc-hold`: the legal-hold worker only |
| `EDISC_S3_DESTROY_ACCESS_KEY` / `_SECRET_KEY` | Identity `edisc-destroy`: destruction only (not built yet) |
| `EDISC_ENV` | `local` here. `production` makes every process refuse dev shortcuts |

The demo and the test stack generate their own env files (`.env.demo`, `.env.test`) from
`.env.example`; they do not read `.env`. A `.env` made before 2026-10-20 lacks the storage
identity variables: copy them from `.env.example`.

The dev stack (`make up`, `make migrate`, `make worker`, `make api`) is in the
[Makefile](Makefile) (`make help` lists every target). It is not needed for the demo or the tests.

## The demo

```
scripts/demo.sh          # from the repo root: a clean start, the whole story, leaves the stack up
scripts/demo.sh down     # removes the demo stack (all its volumes), the report worker and demo-output/
```

It runs on its own disposable Compose project (`edisc-demo`, its own ports) and never touches the
dev or test stacks. It starts a real API process, a collection worker process and a report worker
container, and drives everything through the API.

**Time.** Measured on 2026-10-21 (Apple silicon, report image layers already in Docker's cache):
203 s for the first run in a fresh clone (that run reused an old demo stack's volumes, the bug now
fixed; see [Troubleshooting](#troubleshooting)). A run with nothing cached adds the report image
build (several minutes). A repeat run was measured at 62-70 s on 2026-10-12 (not yet re-measured:
see [the list below](#not-yet-verified-on-a-fresh-clone-disk-guard)).

**The data** is a synthetic Slack export generated by the dummy connector
(`edisc_connector_dummy`): 8 conversations x 5 days, 40 messages per conversation-day, 1,600
messages, 225,240 bytes. The export references 34 attachments on Slack's file host, which cannot be
fetched offline (the worker fetches only from allowlisted public hosts). Each becomes a recorded
gap with its reason (`external_or_hidden`), so the job ends **`completed_with_gaps` by design**:
that is the "no silent data loss" rule working. The spec is `SPEC` at the top of
[scripts/demo.py](scripts/demo.py) (`DatasetSpec(seed=2026, conversations=8, days=5,
messages_per_unit=40, ...)`); change it there to change the dataset. The export is regenerated
from it on every run, byte-identical for the same spec. Fields are documented in
`packages/connectors/dummy/src/edisc_connector_dummy/spec.py`.

| Step | What it does | What to look for | Open in `demo-output/` |
|---|---|---|---|
| 0 | Removes the previous demo stack and output | `N GB free` | |
| 1 | Postgres (FORCE RLS), Redis, MinIO (Object Lock), Temporal; migrations | `schema migrated to 0047` | `logs/compose.log`, `logs/migrate.log` |
| 1b | Builds the report image, measures its PDF toolchain id, starts a report worker container (3 GiB, 1 CPU), starts the API | `PDF toolchain e24aecbb3dd4` (the admitted one) | `logs/report-image.log`, `logs/api.log` |
| 2 | Onboards a tenant with the dev IdP, its admin, a matter | Tenant id from the Host header + token, never a body | |
| 3 | Generates the export, uploads it in parts with `Content-Digest`; the worker hashes, locks and validates it | `1,600 messages; locked in WORM, SHA-256 ...` | |
| 4 | Starts the collection job and **SIGKILLs the worker** about a third of the way; a fresh worker resumes | `SIGKILL worker pid ... with 14/40 units done`, then `completed_with_gaps, sealed` | `logs/worker-1.log` (killed), `logs/worker-2.log` |
| 5 | Reconciliation per conversation-day | `1,600 collected = 1,600 in the export; duplicate versions: 0`; the 34 file gaps with their reason | `summary.json` |
| 6 | Custody chain verified in the database | `84 events, 41 batches, 2044 items, 12 WORM anchors` | |
| 7 | RSMF render through the API, each file downloaded and hash-checked | `40 RSMF files` | `rsmf/*.rsmf`; one copy as `C..._part001of001.eml` to open in Mail |
| 8 | Render package download | Audited and anchored before the first byte | `render.zip` |
| 9 | Collection report requested with a reason | Banner `NOT COMPLETE: status completed_with_gaps; gap 33` | `report/report.pdf` (banner on every page), `report/report.html`, `report/units.jsonl`, `report.zip` |
| 10 | Preview of one conversation-day | CSP `default-src 'none'` | `preview.html` |
| 11 | Job custody package with the export embedded | | `job.zip` |
| 12 | `edisc-verify` on the job, render and report packages, offline | **VERIFIED** three times; `RECOMPUTED from the job chain, identical: units.jsonl byte for byte (41 rows)`; `VERDICT from the job chain alone: NOT clean` (agrees with the report) | `verify.out` |
| 13 | Tamper 1: one unit's collected count +1 in a copy of the report package | **FAILED** is the expected result: `outputs/units.jsonl: bytes do not match the recorded sha256/size` | `report-tampered/` |
| 14 | Tamper 2: one bit flipped in the export inside a copy of the job package | **FAILED** is the expected result: the archive's SHA-256 no longer matches its record, and no entry is read | `job-tampered/` |

Steps 13 and 14 are deliberate tampers: the demo succeeds only if both FAIL (the script checks the
exit codes). Talking points, expected questions and more troubleshooting: [docs/DEMO.md](docs/DEMO.md).

## Local UIs and ports

| | Demo stack | Dev stack (`make up`) | Test stack |
|---|---|---|---|
| API | http://127.0.0.1:18100 (stops when the demo ends) | http://localhost:8000 (`make api`) | in-process |
| Temporal UI | http://localhost:28080/namespaces/edisc/workflows | http://localhost:8080/namespaces/edisc/workflows | :18080 |
| MinIO console | http://localhost:29001 | http://localhost:9001 | :19001 |
| MinIO S3 / Postgres / Redis / Temporal | 29000 / 25432 / 26379 / 27233 | 9000 / 5432 / 6379 / 7233 | 19000 / 15432 / 16379 / 17233 |

- **Temporal UI:** our namespace is **`edisc`**. The UI opens on `default`, which does not exist
  here ("Namespace default is not found"): use the links above or pick `edisc` top left. Worth
  seeing: one `CollectionJobWorkflow` per job with a `CollectUnitWorkflow` child per
  conversation-day, `ExportIngestWorkflow`, `RenderWorkflow`, `ReportWorkflow`; in a job's history,
  the activity attempts retried after the SIGKILL.
- **MinIO console:** log in with `EDISC_S3_ROOT_USER` / `EDISC_S3_ROOT_PASSWORD` from the stack's
  env file (`.env.demo` for the demo, `.env` for the dev stack). Never share or commit those files.
  Worth seeing: bucket `edisc-evidence` (versioning on, Object Lock COMPLIANCE), the
  content-addressed files under `t/<tenant>/files/sha256/`, the anchors, and each object's
  retention mode and date.
- **API:** every tenant route needs the tenant's Host subdomain and a token; the contract is
  [docs/api/openapi.json](docs/api/openapi.json). [scripts/demo.py](scripts/demo.py) shows a
  complete client.

## Verifying offline

An outside expert needs only the source at the named commit, uv, and the packages:

```
git clone https://github.com/Mihir1107/kickoff.git && cd kickoff
uv sync --frozen --no-dev --package edisc-renderers
uv run --no-sync edisc-verify job.zip
uv run --no-sync edisc-verify render.zip --job-package job.zip
uv run --no-sync edisc-verify report.zip --job-package job.zip
uv run --no-sync edisc-verify job.zip --json       # machine-readable result
```

- **VERIFIED** (exit 0): every file, chain event, Merkle root, item, evidence object and anchor
  checked. With `--job-package`, the report's `units.jsonl` and chain-derived sections are
  recomputed from the job chain and must be identical. Read the counts under the verdict: they say
  what was actually checked.
- **FAILED** (exit 1): every `ERROR` line names where and what did not hold.
- Exit **2**: the input could not be read (not a package). Nothing was verified.

The expert's guide, including what is recomputed vs checked by digest only and how to fetch
anchors independently: [docs/VERIFYING.md](docs/VERIFYING.md).

## Tests

```
make check                  # lint, mypy --strict, unit tests; no services (978 passed, 23 skipped, about 2.5 min)
make test-integration       # a FRESH ephemeral stack (project edisc-test), every integration test, then destroyed
make test-env-up            # or keep a test stack up while iterating:
make test-integration-only TESTS=tests/integration/renders
make test-env-down
```

- Integration tests use real Postgres, MinIO and Temporal: never mocks, never the dev stack. The full
  suite takes about 20 minutes on a laptop. Each test has a 120 s timeout, and every test fails if
  an event loop was blocked longer than 250 ms.
- **Logs:** every make test target writes `test-runs/<UTC time>-<label>/output.log` (plus failure
  artifacts and the stack's logs on failure); `test-runs/latest` is the newest. Read that, not the
  terminal.
- **CI** ([.github/workflows/ci.yml](.github/workflows/ci.yml)): `lint`, `typecheck`, `unit` (also
  proves the shards cover every test exactly once), `api-compat` (breaking-change check of the
  OpenAPI document), `production-readiness` (warns on main, fails on release tags), `integration`
  in 4 shards balanced by measured duration (`scripts/ci_shard.py`), `production-install` (every
  service from a no-dev install), `report-pdf` (inside the report image: PDF goldens, veraPDF). A
  failed integration shard uploads worker logs, Temporal histories and custody streams.
- **Mutation testing** ([scripts/mutation/README.md](scripts/mutation/README.md)): 650 catalogued
  breaks, each an exact edit plus the test that must fail. `uv run python scripts/mutation/run.py
  --check` (edits still apply), `--kind unit` (no services; 13 minutes in a fresh clone),
  `--kind integration` (test stack), `--round <name>` (one round).
- **Goldens** ([tests/golden/README.md](tests/golden/README.md)): byte-exact expected outputs
  (RSMF, reports, PDFs, Temporal histories, the dummy dataset), keyed by the versions that produce
  them. Recorded only by hand after a deliberate version bump (`EDISC_RECORD_RSMF=1`,
  `EDISC_RECORD_REPORT=1`, `EDISC_RECORD_REPORT_PDF=1` inside the image, ...). Never in CI (refused
  there) and never overwritten: a golden recorded by the code under test would prove nothing.

### Not yet verified on a fresh clone (disk guard)

The handover check on 2026-10-21 ran every other command in this README from a fresh clone. These
were not run, because the machine had 9 GB free and the guards need 10 (demo) or 15 (make); they
are taken from the Makefile and earlier sessions' records:

- a warm (second) `scripts/demo.sh` run and its time
- `make up`, `make migrate`, `make worker`, `make api`, `make down` (the dev stack), and the dev
  stack's ports and URLs in [Local UIs and ports](#local-uis-and-ports)
- the MinIO console login
- `make test-integration` (it was run, and the disk guard refused it), `make test-env-up`,
  `make test-integration-only TESTS=...`, `make test-env-down`
- `uv run python scripts/mutation/run.py --kind integration`
- recording goldens (`EDISC_RECORD_*`)

## Architecture in brief

```
 user ──▶ apps/api (FastAPI) ──start job──▶ Temporal ──▶ workers/collection
            │ tenant from Host + token          CollectionJobWorkflow
            │ RLS per transaction                └ CollectUnitWorkflow per conversation-day
            ▼                                         │
         Postgres  ◀───────────────────────────────────┤
                                                       ▼
   one batch:  connector fetch (rate-limited, Redis) ─▶ raw page bytes
               ─▶ evidence: SHA-256 while streaming ─▶ MinIO/S3 Object Lock (WORM)
               ─▶ normalizer: versioned items with idempotency keys
               ─▶ ONE Postgres transaction: items + job links + custody event (Merkle root)
                  + checkpoint + counts
               ─▶ anchor the chain to WORM when due
```

A crash before the transaction commits replays the page from the old checkpoint (items deduplicate
by key); a crash after it resumes from the new one. When every unit is settled, the job reconciles,
sets its status and seals its chain. Renders and reports run as their own workflows with their own
custody streams. The full map: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repository map

| Path | What |
|---|---|
| `apps/api` | `edisc_api`: FastAPI service (tenants, matters, connections, exports, jobs, renders, reports, holds) |
| `apps/web` | Placeholder; the frontend lives on branch `feat/web-ui` |
| `workers/collection` | `edisc_worker`: Temporal workflows and activities, the pipeline, render/report loaders, sweepers |
| `packages/core` | `edisc_core`: settings, canonical JSON, ids, UTC time, KMS and envelope encryption, redaction, loop guard |
| `packages/db` | `edisc_db`: Alembic migrations (hand-written SQL), models, roles, `tenant_tx` sessions |
| `packages/evidence` | `edisc_evidence`: WORM evidence writer and range reader (S3 Object Lock) |
| `packages/custody` | `edisc_custody`: hash chain, Merkle roots, anchors, packages, `edisc-verify`, hardened ZIP reader and writer, legal hold |
| `packages/connectors/base` | Connector protocol, work units, the rate limiter |
| `packages/connectors/dummy` | Deterministic fake source: the golden dataset and its oracle; also writes synthetic Slack exports |
| `packages/connectors/slack_export` | Slack export ZIP layout, tier detection, connector, file-link fetching |
| `packages/connectors/slack`, `teams` | Stubs for the Phase 3 live connectors |
| `packages/normalizer` | `edisc_normalizer`: raw pages to versioned items (pure) plus persistence |
| `packages/renderers` | `edisc_renderers`: RSMF renderer, report model/HTML, preview (pure, vendored RSMF schema) |
| `infra` | Docker Compose (images pinned by digest), MinIO identity policies, Postgres and Temporal init, AWS policy drafts |
| `deploy/report-image` | The pinned linux/amd64 image that renders report PDFs |
| `scripts` | Demo, env generators, measurements, CI sharding, API compatibility, mutation runner, git hooks |
| `spikes` | The PDF reproducibility spike behind ADR 0018 |
| `tests` | `unit` (no services), `integration` (real services), `golden`, `real_idp` |
| `docs` | Architecture, ADRs, plans, measurement runs, demo, verifier guide, handoff, backlog |

Read first: [CLAUDE.md](CLAUDE.md) (the rules), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/HANDOFF.md](docs/HANDOFF.md) (state and next steps),
`workers/collection/src/edisc_worker/pipeline.py` (the exactly-once batch),
`packages/custody/src/edisc_custody/log.py` (the custody chain),
`packages/evidence/src/edisc_evidence/writer.py` (WORM writes), [scripts/demo.py](scripts/demo.py)
(the whole flow as an API client).

## Key concepts

| Term | Meaning |
|---|---|
| Unit | One conversation on one UTC day (`<conversation>/<YYYY-MM-DD>`): the unit of work, checkpointing and reconciliation (ADR 0005) |
| Reconciliation status | Per unit: `matched`, `gap`, `surplus`, `unverifiable`, `failed`, `access_lost`, `matched_against_archive` (complete relative to an export only), `not_applicable`, `pending`. Job status follows: `completed` only if every unit is matched; otherwise `completed_with_gaps`, `completed_unverified`, `completed_with_failed_units`, `completed_against_archive`, `failed`, `cancelled` |
| WORM / Object Lock | Write once, read many. S3 Object Lock in COMPLIANCE mode: no one, root included, can change or delete a locked version before its retention date |
| Version pinning | Every read of evidence names the exact S3 VersionId recorded at write time, so a later version under the same key can never be read by mistake |
| Custody chain | Per stream (job, render, report, tenant audit): `event_hash = sha256(prev_hash + canonical_json(event))`; append-only in the database |
| Merkle root | RFC 6962 root over a batch's items (sorted by idempotency key), stored in the batch's custody event: proves exactly which items the batch committed |
| Anchor | A copy of a chain head (seq + hash) written to WORM. A rewritten chain disagrees with its anchors |
| Seal | The final anchor of a finished stream, naming its exact head |
| Idempotency key | `tenant + source + source_item_id + content_hash`: re-collecting is a no-op, an edit is a new version |
| Render | One RSMF production of a sealed job (its own workflow and custody stream) |
| Production | The stored, locked output files of a render (evidence kind `production`) |
| RSMF | Relativity Short Message Format: an EML file holding a JSON manifest and attachments, one per conversation-day part |
| Version triple | Renderer version, Unicode version, tzdata version: a render's bytes are promised for its inputs plus this triple (ADR 0015, 0017) |
| Byte identity | Same inputs + same versions = same bytes, for RSMF, reports and packages; goldens hold the expected bytes |
| Tenant | One customer. Every tenant table has FORCE row-level security; the tenant comes from the Host subdomain and the token, never from a request body |
| Legal hold | A hold on a client or matter: blocks closing, retention shortening and destruction; S3 legal-hold flags on the stored objects (ADR 0019) |
| Oracle | Expected results computed independently of the code under test (from the dummy `Dataset`), never from collected output |
| Mutation testing | Deliberately breaking one protection to prove a named test fails (`scripts/mutation/`) |

## Decision records

All in [docs/adr/](docs/adr/); [docs/README.md](docs/README.md) indexes every document.

| ADR | Decides |
|---|---|
| [0001](docs/adr/0001-temporal-workflow-engine.md) | Temporal is the workflow engine |
| [0002](docs/adr/0002-worm-evidence-storage.md) | Evidence in S3 Object Lock COMPLIANCE, streaming SHA-256, rolling retention |
| [0003](docs/adr/0003-custody-hash-chain.md) | Custody as a hash chain with Merkle-rooted batches and WORM anchors |
| [0004](docs/adr/0004-idempotency-and-versioning.md) | Idempotency key and version fingerprint; edits are new versions |
| [0005](docs/adr/0005-unit-of-work-and-reconciliation.md) | Unit = conversation x UTC day; reconciliation statuses; absence is never deletion |
| [0006](docs/adr/0006-exactly-once-batch-transaction.md) | Exactly-once effects: one transaction per batch, checkpoint inside it |
| [0007](docs/adr/0007-tenant-isolation-rls.md) | Tenant isolation with Postgres FORCE RLS and a non-owner app role |
| [0008](docs/adr/0008-custody-package-and-offline-verifier.md) | Custody package format and the offline verifier |
| [0009](docs/adr/0009-token-envelope-encryption.md) | Per-tenant envelope encryption of connection tokens |
| [0010](docs/adr/0010-distributed-rate-limiting.md) | Distributed rate limiting with Redis token buckets |
| [0011](docs/adr/0011-thread-parent-policy.md) | Thread replies whose parent is out of range (Proposed: default pending owner sign-off) |
| [0012](docs/adr/0012-temporal-workflow-design.md) | Workflow fan-out, continue-as-new, error classes, cancellation, versioning |
| [0013](docs/adr/0013-api-authn-authz.md) | API authentication and scoped roles (tenant > client > matter > workspace) |
| [0014](docs/adr/0014-slack-export-ingestion.md) | Slack export ingestion: upload, lock, validate, tiers, collection |
| [0015](docs/adr/0015-rsmf-renderer.md) | RSMF renderer: slicing, byte identity, natives, custody; §23-§24 hold the loop and performance findings |
| [0016](docs/adr/0016-session-auth.md) | Browser sessions, CSRF, re-authentication, source install flows, access lost |
| [0017](docs/adr/0017-render-worker-versions.md) | Running old renderer/Unicode/tzdata triples over time (version-keyed queues) |
| [0018](docs/adr/0018-collection-report-and-preview.md) | Collection report (JSON, HTML, reproducible PDF) and preview; §31 lists every deviation |
| [0019](docs/adr/0019-legal-hold-and-destruction.md) | Storage identities, upload quotas, legal hold, destruction |

## Rules for contributors

[CLAUDE.md](CLAUDE.md) is binding; read it in full before changing the core. Its non-negotiables:
no silent data loss (gaps fail loudly, no swallowed exceptions); application code never modifies raw
evidence; every action is a custody event; idempotency by key; any job survives SIGKILL with zero
gaps and zero duplicates; tokens never leave us (encrypted, never logged, never in Temporal);
connectors only fetch raw, interpretation is in the normalizer; stream, never load. Also: mypy
`--strict`, uv only, one milestone at a time, never `--no-verify`, never loosen a guard or a
threshold to make a test pass, and an intermittent failure is a bug until diagnosed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `refusing: only N GB free on this disk, need >= 15 GB` (make) or `refusing: N GB free, need 10` (demo) | Free disk. Do not lower the guard: locked test evidence cannot be deleted, and a full disk makes Docker's VM read-only |
| `failed to connect to the docker API` | Start Docker Desktop (`open -a Docker`) and wait until it reports running |
| `removing the edisc-demo stack another checkout left: ...` | Expected: the demo (and `scripts/demo.sh down`) removes every container, volume and network of compose project `edisc-demo`, whichever checkout started it, so a run never reuses an old stack |
| `refusing: the edisc-demo stack (containers, volumes or networks) could not be removed` | Something holds part of the old stack. The message names the `docker ps -a` / `docker volume ls` / `docker network ls` filter to find it; remove what it lists, then run the demo again |
| A second test run fails fast with a lock message | Another run owns the `edisc-test` stack (`scripts/stack_lock.py`). Wait for it, or `make test-env-down` if it is stale |
| `report image build failed` | Read `demo-output/logs/report-image.log`. Docker Hub pulls can stall for minutes at `FROM`: do not cancel. Under emulation the report's PDF takes longer; see `demo-output/logs/report-worker.log` |
| Temporal UI shows "Namespace default is not found" | Switch to namespace `edisc` (links in [Local UIs and ports](#local-uis-and-ports)) |
| Port already in use | Another stack is up: `scripts/demo.sh down` (demo ports), `make down` (dev), `make test-env-down` (test) |

More in [docs/DEMO.md](docs/DEMO.md) and the "Gotchas" section of [docs/HANDOFF.md](docs/HANDOFF.md).

## Open questions and next work

Requested by the platform owner (all **not started**):

| Item | Notes |
|---|---|
| Options on ZIP export collections: channels, conversation types, date range | Jobs already take scopes (type, conversation id or `*`, date range); not built: choosing by conversation type (public, private, DM, group DM) and an options surface for exports |
| Daily or weekly slicing | Units and RSMF slices are per conversation-day today; weekly would be a render option in the render identity |
| Parent/child families, including nested attachments | Threads follow ADR 0011; attachments are natives of their message; family ids and nested attachments are not modelled |
| RSMF validation against Relativity | Today: the vendored RSMF 2.0 JSON schema plus structural checks. Relativity's validator needs a licence; open: DEFLATE vs STORED and the 2 GB limit (HANDOFF "Open decisions" 2) |
| Forensic images (E01/AD1) | Later |
| Teams via an Azure app and Microsoft Graph | Later; the consent flow and token handling exist (M17 step 6), the connector is a stub |

Open question: deliver the core as a **library** the platform calls, or as a **standalone
service** (its own API, workers and storage, as built). The custody and verification code is
already split into pure libraries; the pipeline assumes its own Postgres, Temporal and bucket.

Next engineering work (from [docs/HANDOFF.md](docs/HANDOFF.md) "Next" and
[docs/BACKLOG.md](docs/BACKLOG.md)):
1. Merge M18 step 5: record the PDF goldens for toolchain `1e861ea7e088` inside the report image
   on a machine with 15 GB free, then fast-forward `main`.
2. M18 step 6 (destruction) and step 7 (destruction certificate).
3. Real Slack exports as fixtures; real IdP and app confirmation.
4. Cloud items marked "required before production" in the backlog (AWS KMS, bucket policies,
   report image registry, 1M-message soak).
5. Decisions waiting on the owner: report paper size, whether reviewers see names (ADR 0018 §17),
   ADR 0011's default, ADR 0013 (a) and (c).

## Licence
Proprietary and confidential: see [LICENSE](LICENSE). The vendored RSMF schema in
`packages/renderers/src/edisc_renderers/rsmf/schema/` keeps its own BSD-3 licence.
