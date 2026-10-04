# eDiscovery collection module

[![ci](https://github.com/Mihir1107/kickoff/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Mihir1107/kickoff/actions/workflows/ci.yml)

This service collects workplace communications (Slack today, Teams and others later) for litigation
and investigations, so that every collection is **complete, provable and tamper-evident**. Data is
organized by tenant, then client, then matter and workspace, as in Relativity. Every collected item is
hashed and locked in WORM storage. Every action is appended to a hash-chained custody log that a
third party can verify offline. Gaps are reported, never hidden: a collection that missed anything
is never "completed". Collections are delivered as RSMF for review platforms, plus native JSON
and a collection report.

## Architecture in brief
- **API** (`apps/api`, FastAPI): the tenant comes from the Host subdomain and the identity provider
  token. Roles are scoped. Every content read is audited.
- **Workers** (`workers/collection`, Temporal): one workflow per job that fans out to one per
  conversation-day unit. A worker can be SIGKILLed anywhere and the job resumes from its database
  checkpoint with no gaps and no duplicates.
- **Storage:** Postgres with FORCE row-level security per tenant. S3 Object Lock in COMPLIANCE mode
  (MinIO locally) holds the evidence and the custody anchors. Redis runs the distributed rate limiter.
- **Pipeline:** connectors only authenticate, enumerate and fetch raw bytes. The normalizer turns raw
  pages into versioned items (an edit is a new version, never an overwrite). Each batch commits its
  items, links, custody event and checkpoint in one transaction. Reconciliation compares expected
  against collected for every conversation-day.
- **Outputs:** an offline custody package checked by `edisc-verify`, and RSMF 2.0 renders validated
  against Relativity's published schema, byte-identical for the same inputs.

The full map is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); each decision has an ADR in [docs/adr/](docs/adr/).

## Prerequisites
- Docker (Docker Desktop on macOS)
- [uv](https://docs.astral.sh/uv/) (installs the pinned Python 3.12 itself)
- 15 GB of free disk (the make targets refuse to start below that)

```
uv sync --all-packages
make hooks        # once per clone: ruff + mypy on staged files
```

## One-command demo
```
scripts/demo.sh          # about 30 s from clean on its own disposable stack
scripts/demo.sh down     # remove it
```
It starts the stack and seeds a tenant. It uploads a synthetic Slack export and collects it while
SIGKILLing the worker part way through. It then shows reconciliation and custody, verifies the
offline package (VERIFIED), flips one bit (FAILED) and renders RSMF. See [docs/DEMO.md](docs/DEMO.md).

## Tests
```
make check                 # lint, mypy --strict, unit tests (no services needed)
make test-integration      # a fresh ephemeral stack, all integration tests, then destroyed
make test-env-up           # or keep a test stack up while iterating:
make test-integration-only TESTS=tests/integration/renders
make test-env-down
```
Integration tests use real Postgres, MinIO and Temporal. They never mock them and never run against
the dev stack.

## Repository layout
```
apps/api/              FastAPI service (edisc_api)
workers/collection/    Temporal workflows and activities, render loader and storage (edisc_worker)
packages/core          settings, canonical JSON, ids, time, KMS/envelope, redaction
packages/db            schema (Alembic migrations), models, sessions, roles
packages/evidence      WORM evidence writer and reader (S3 Object Lock)
packages/custody       hash chain, Merkle roots, anchors, custody package, edisc-verify, archive reader
packages/connectors/   base protocol and rate limiter; dummy (golden dataset), Slack export, stubs
packages/normalizer    raw pages to versioned items (pure) plus persistence
packages/renderers     RSMF renderer (pure), with the vendored RSMF schema
infra/                 docker compose (pinned images)
scripts/               demo, measurements, env generators, git hooks
tests/                 unit, integration (real services), golden data
docs/                  architecture, ADRs, plans, measurement runs, handoff, demo
```

## Documentation
- [docs/README.md](docs/README.md): index of every document, with ADR statuses
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/adr/](docs/adr/), [docs/HANDOFF.md](docs/HANDOFF.md) (current state and next steps)
- [docs/DEMO.md](docs/DEMO.md), [docs/BACKLOG.md](docs/BACKLOG.md), [CLAUDE.md](CLAUDE.md) (engineering rules)

## Licence
Proprietary and confidential: see [LICENSE](LICENSE). The vendored RSMF schema in
`packages/renderers/src/edisc_renderers/rsmf/schema/` keeps its own BSD-3 licence.
