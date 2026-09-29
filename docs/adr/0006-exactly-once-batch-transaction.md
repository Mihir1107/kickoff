# ADR 0006: Exactly-once effects via one transaction per batch

Status: Accepted (2026-09-30)

## Context
Workers can be SIGKILLed at any instruction. Temporal gives at-least-once activity execution; we need
exactly-once *effects*.

## Decision
Per fetched page:
1. WORM write (idempotent: write-ahead row + `If-None-Match`). May be repeated; orphans are accounted.
2. Single Postgres transaction:
   `INSERT items ON CONFLICT (idempotency_key) DO NOTHING` → `INSERT job_items ON CONFLICT DO NOTHING` →
   append custody `items_collected` (chain head row lock) → update `work_units` cursor and
   `collected_count`. Cursor advance is conditional on the expected previous cursor (optimistic check),
   so two concurrent executions of the same unit cannot both advance.
3. Commit, then heartbeat.

## Consequences
- + Kill before commit ⇒ page replayed from old cursor; kill after ⇒ resume from new cursor. No gaps, no dupes.
- − Chain-head lock is held for the transaction; mitigated by per-batch events (ADR 0003). Measured in M14.
