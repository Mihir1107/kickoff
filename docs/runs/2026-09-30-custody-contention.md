# Custody head-lock contention (M5), 2026-09-30

**Setup:** local compose (Postgres 16.15, Docker Desktop, 8 CPUs / 8 GB, Apple Silicon). One Python
process drove it with `scripts/bench_custody_contention.py --writers 1 4 16 32 --batches 40 --items 100`.

**What each transaction does:** it is the real M11 batch commit minus S3: 100 `items` inserts,
`append_batch` (which takes the head row lock), 100 `job_items` links, and a commit.
- **one shared:** all writers append to one job chain. This is today's design: many child workflows,
  one job.
- **one per writer:** every writer has its own chain. This is the upper bound per-unit chains could
  reach.

| writers | chain | batches/s | msgs/s | p50 ms | p95 ms |
|---|---|---|---|---|---|
| 1 | one shared | 73 | 7311 | 13.4 | 15.2 |
| 1 | one per writer | 70 | 6995 | 14.4 | 15.4 |
| 4 | one shared | 182 | 18176 | 18.1 | 55.8 |
| 4 | one per writer | 226 | 22590 | 15.6 | 20.0 |
| 16 | one shared | 185 | 18541 | 82.8 | 143.6 |
| 16 | one per writer | 308 | 30816 | 48.8 | 64.0 |
| 32 | one shared | 172 | 17220 | 140.4 | 432.0 |
| 32 | one per writer | 289 | 28950 | 108.1 | 137.2 |

## Reading
- **The shared chain serializes.** Throughput plateaus at about 180 batches/s (about 18k messages/s
  per job) from 4 writers up, and p95 latency grows with the number of writers. The lock is held from
  `append_batch` until commit: the 100 `job_items` inserts plus the commit fsync.
- **Separate chains scale further.** About 300 batches/s here, where the single-process benchmark
  client is itself the limit.
- **It is not a hotspot for live sources.** One job's ceiling is about 18k messages/s.
  - *Correction (review, 2026-09-30):* Slack internal apps get 50+ requests/min on
    `conversations.history` at up to 1000 messages/request, so roughly **830 messages/s per token**.
    Graph is in the same order of magnitude.
  - That is still about 20× below the shared-chain ceiling, so the conclusion is unchanged.
  - The cap only matters for offline ingestion (Phase 2 Slack export zips) or the dummy connector
    (1M messages in about 1 minute of DB time, which is acceptable).

## Decisions (review, 2026-09-30)
- Proposal 1 (lock-narrowing reorder) **will be implemented when Phase 2 bulk export ingestion lands**,
  followed by a re-measurement.
- Proposal 2 (per-unit chains) stays deferred.

## Proposals
1. **Cheap, no model change:** shorten the lock hold. Make `fk_job_items_…_custody_events`
   `DEFERRABLE INITIALLY DEFERRED`, pre-allocate the event id, insert `job_items` first, and call
   `append_batch` last, just before commit. The lock then covers one insert, one update and the
   commit. Expected: the shared-chain ceiling moves toward the per-writer numbers.
2. **Only if (1) is insufficient:** per-work-unit chains, rolled up into the job chain. Each unit keeps
   its own chain. When a unit finishes, its head (unit_key, seq, hash) goes into one
   `unit_chain_sealed` event on the job chain. Anchoring and verification recurse. This raises the
   ceiling to per-writer levels, at the cost of more streams and anchors and a two-level verifier.

Re-measure at M14 with the full pipeline and 10 worker processes.
