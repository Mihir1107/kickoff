# ADR 0001: Temporal as the workflow engine

Status: Accepted (2026-09-30)

## Context
Collections run for hours to days, touch rate-limited APIs, and must survive worker crashes, deploys
and restarts with zero gaps and zero duplicates. We need durable orchestration, retries with backoff,
bounded fan-out, and visibility into what is running.

## Decision
- Use Temporal (Python SDK `temporalio`). Self-hosted `temporalio/server` on Postgres persistence locally
  and in CI (schema applied by `admin-tools`, matching production topology), UI at :8080.
- One task queue per source platform (`collect-<source>`), so a noisy source cannot starve another and
  workers can be scaled/pinned per connector version.
- **Temporal is orchestration only, never a data path.** Workflow inputs/outputs are IDs, small cursors and
  counters. Raw payloads, tokens and item lists never enter history (history is persisted and replayed).
- **The database checkpoint is the source of truth for resume**, not heartbeat details. Activities
  heartbeat for liveness only; on retry they reload the cursor from `work_units`.
- Parent `CollectionJobWorkflow` reads the unit list from the DB in pages and runs a bounded number of
  `CollectUnitWorkflow` children; both use continue-as-new to cap history size.
- Activities are idempotent: every side effect is either a WORM write guarded by `If-None-Match` +
  write-ahead registry, or a DB write guarded by unique keys inside one transaction.

## Consequences
- + Crash/kill safety, retries and visibility for free; deterministic replay forces discipline.
- − Workflow code must stay deterministic (sandbox; `workflow.now()`, no I/O). Pydantic/structlog imports
  need sandbox pass-through.
- − Extra infrastructure (server + Postgres DBs). Accepted: we would otherwise rebuild a worse version.
