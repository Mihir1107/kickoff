# Documentation index

Start with [HANDOFF.md](HANDOFF.md) (current state, open decisions, next milestone) and
[ARCHITECTURE.md](ARCHITECTURE.md) (the map). Engineering rules are in [../CLAUDE.md](../CLAUDE.md).

## Architecture Decision Records ([adr/](adr/))
Statuses as of 2026-10-04, taken from each ADR's status line.

| # | Title | Status |
|---|---|---|
| [0001](adr/0001-temporal-workflow-engine.md) | Temporal as the workflow engine | Accepted |
| [0002](adr/0002-worm-evidence-storage.md) | WORM evidence storage (S3 Object Lock, COMPLIANCE) | Accepted, implemented (M6) |
| [0003](adr/0003-custody-hash-chain.md) | Chain of custody as a hash chain with Merkle-rooted batches and WORM anchors | Accepted, implemented (M5) |
| [0004](adr/0004-idempotency-and-versioning.md) | Idempotency key and version fingerprint | Accepted (amended 2026-10-02, 2026-10-04) |
| [0005](adr/0005-unit-of-work-and-reconciliation.md) | Unit of work = conversation × UTC day; reconciliation semantics | Accepted |
| [0006](adr/0006-exactly-once-batch-transaction.md) | Exactly-once effects via one transaction per batch | Accepted, implemented (M11) |
| [0007](adr/0007-tenant-isolation-rls.md) | Tenant isolation with Postgres row-level security | Accepted, implemented |
| [0008](adr/0008-custody-package-and-offline-verifier.md) | Custody package format and offline verifier (`edisc-verify`) | Accepted, implemented (M5) |
| [0009](adr/0009-token-envelope-encryption.md) | Envelope encryption for connection tokens | Accepted, implemented (M7) |
| [0010](adr/0010-distributed-rate-limiting.md) | Distributed rate limiting (Redis token buckets) | Accepted, implemented (M8) |
| [0011](adr/0011-thread-parent-policy.md) | Thread replies in range whose parent is out of range | **Proposed** (default pending product-owner confirmation; implemented as a configurable policy) |
| [0012](adr/0012-temporal-workflow-design.md) | Temporal workflow design: fan-out, continue-as-new, errors, cancellation, versioning | Accepted, with review changes R1–R6 |
| [0013](adr/0013-api-authn-authz.md) | API authentication and authorization (tenant > client > matter > workspace) | Accepted; decisions (a) and (c) pending product-owner confirmation |
| [0014](adr/0014-slack-export-ingestion.md) | Slack export ingestion | Accepted, with review changes R1–R7; implemented (M14) |
| [0015](adr/0015-rsmf-renderer.md) | RSMF renderer | Accepted; steps 1–3 implemented, steps 4–5 next (M15) |
| [0016](adr/0016-session-auth.md) | Browser sessions, CSRF, re-authentication and source install flows | Accepted, not implemented yet (M17) |

## Plans ([plans/](plans/))
- [plans/m13-api.md](plans/m13-api.md): the M13 collection API plan (implemented, M13.1 to M13.7).
- [plans/phase-2.md](plans/phase-2.md): Phase 2: Slack export ingestion, RSMF, preview and report,
  minimal UI. The decisions were recorded on 2026-10-02. M14 is done and M15 is in progress (see
  HANDOFF for the current state).

## Measurement runs ([runs/](runs/))
How runs are recorded: [runs/README.md](runs/README.md).
- [2026-09-30 custody contention](runs/2026-09-30-custody-contention.md): custody head-lock contention (M5)
- [2026-10-01 audit burst](runs/2026-10-01-audit-burst.md): the tenant audit chain under a burst of evidence downloads
- [2026-10-01 resume soak](runs/2026-10-01-resume-soak.md): SIGKILL resume soak (M12 acceptance)
- [2026-10-01 storage and throughput](runs/2026-10-01-storage-throughput-breakdown.md): storage per component and per-stage throughput (10k messages)
- [2026-10-02 export range reads](runs/2026-10-02-export-range-reads.md): range requests per 1,000 archive entries (ADR 0014 R6)

## Other documents
- [DEMO.md](DEMO.md): the one-command demo (`scripts/demo.sh`) and what each step shows.
- [HANDOFF.md](HANDOFF.md): state, decisions, open questions, next milestone, gotchas.
- [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, principles.
- [BACKLOG.md](BACKLOG.md): later-phase work and items required before production.
- [reports/](reports/): progress reports (PDF).
- Vendored RSMF schema provenance:
  [SOURCE.md](../packages/renderers/src/edisc_renderers/rsmf/schema/SOURCE.md).
