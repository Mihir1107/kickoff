# ADR 0006: Exactly-once effects via one transaction per batch

Status: Accepted (2026-09-30), implemented in M11 (`edisc_worker.pipeline`, migration 0011)

## Context
Workers can be killed at any instruction, and Temporal runs activities at least once: a zombie attempt
can still be running while its retry starts. We need exactly-once **effects**.

## Decision
Per fetched page (`Pipeline.process_batch`):
1. **Network I/O first, outside any transaction.** The page is written to WORM and completed. Every
   file it references is written, or its refusal is recorded (`FileUnavailable`). The transaction only
   references completed evidence. A crash here leaves only accounted, complete orphans.
2. **One short transaction** (one page, far below `lock_timeout`):
   - lock the work unit `FOR NO KEY UPDATE`. The NO KEY form leaves foreign-key checks from
     `job_items` unblocked;
   - **checkpoint guard:** if the stored cursor is no longer the cursor this batch started from, the
     batch was already applied, so return with **no writes at all**;
   - load prior state, normalize (pure), persist items (idempotent);
   - insert `job_items` links under a **pre-allocated custody event id** (the FK to `custody_events`
     is `DEFERRABLE INITIALLY DEFERRED`), learning exactly which links are new;
   - append the `items_collected` event, whose Merkle root covers exactly those new links;
   - advance the cursor, `pages_done` and `file_gaps`, and record the last history page.
3. **After commit:** anchor the custody head if due.

A `None` cursor with `pages_done > 0` means "all pages applied", so a resume goes straight to finalize
and never re-fetches the unit.

## Verification (`tests/integration/pipeline`)
- **Crash matrix:** a simulated kill at each boundary (after evidence write, mid-transaction, after
  commit before return, during unit/job finalize), at the 1st and 5th occurrence, in two consecutive
  epochs. Each resumed job ends with:
  - results exactly equal to the oracle;
  - zero duplicate items and no dangling links;
  - a valid custody chain with its seal;
  - no pending evidence rows.
- A resumed job records exactly the same number of custody batch events as a clean job.
- **Two concurrent executors** of the same units (zombie plus retry) produce the clean result.
  Mutation-checked: without the checkpoint guard this test fails.

## Consequences
- + Kill before commit means the page is replayed from the old cursor. Kill after commit means the
  job resumes from the new cursor. A concurrent duplicate is a no-op.
- − The work-unit row lock serializes executors of one unit (intended). The custody head lock
  serializes batches of one job (measured; see `docs/runs/2026-09-30-custody-contention.md`).
