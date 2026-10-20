# Export validation on stale statistics: diagnosis, fix, 20M-entry estimate (2026-10-19)

ADR 0015 §24.20 has the diagnosis. This note has the numbers.

## Before / after, the zip64 test (66,004 entries, `export_entries.idx_tup_fetch` in validation)

| Run | Code | Validation | Tuples fetched | Notes |
|---|---|---|---|---|
| laptop | before | 9.7 s | 7,912,240 | page query quadratic (Sort over the remainder), FK checks mostly on the primary key (the plan cache switched to generic after 5) |
| CI 39132450451 | before (instrumented) | > 60 s (timeout) | **643,353,122** at 40,000 of 66,000 day files | FK checks stayed on `(export_id, kind, folder)` reading up to each row: n²/2 |
| laptop | after | 7.4-8.4 s | 213,722 | every sampled FK check on `pk_export_entries` (5 blocks), pages on the ordered index (3 ms each, flat) |
| CI 39260189440 | after | 10.2 s | 178,006 | day-file inserts flat (about 23 ms each, first to last), every sampled FK check on the primary key, waits CPU only |

## Linearity of the fixed code (laptop, same session, the export worker in process)

| Entries | Validation | Per entry | Tuples fetched | Per entry | Day-file inserts | Page reads |
|---|---|---|---|---|---|---|
| 66,004 | 7.53 s | 114 µs | 176,006 | 2.7 | 133 statements, 1.99 s | 0.48 s |
| 330,004 | 37.38 s | 113 µs | 1,330,216 | 4.0 | 661 statements, 10.17 s | 1.89 s |

- **Time per entry is flat.** The ANALYZE of `export_entries` costs 0.2 s at either size: its sample
  is bounded.
- **Tuples fetched per entry rise from 2.7 to 4.0.** That is a residual to watch, still far under the
  test's bound of 15.

## Estimate for a 20M-entry export

**New code:** 20,000,000 × 113 µs ≈ **38 minutes** on this laptop. CI-class hosts ran this
validation 1.5-2x slower on fast runs, so about 1-1.3 h there. Plus an ANALYZE: constant in table
size (a 30,000-row sample), measured 0.2 s here. Not included:
- storage reads of a multi-GB archive (the range reads, ADR 0014: measured separately);
- deeper indexes (logarithmic).

**Old code**, two quadratic terms, extrapolated by (20M / 66k)² ≈ 91,800:
- **Page query** (every plan state, the laptop's case): 2.73 s of page reads at 66k → ≈ 250,000 s ≈
  **2.9 days**.
- **FK checks on the bad plan** (CI's case): n²/2 = 2 × 10¹⁴ index tuples at the CI rate of
  ~85 ns per tuple (643 M in ~55 s) → ≈ 1.7 × 10⁷ s ≈ **200 days**.

So the old code needed between about 3 days and about 200 days, depending on which plan the
backends kept. The new code needs under an hour and a half.

## The sweep of other bulk-load paths (auto_explain, 2 % of top-level statements with their nested ones, 38,272 plans)
Suites: pipeline, worker, acceptance (50k SIGKILL soak), corpus, renders, reports, directory, corpus
export, normalizer, export variants. 288 passed.

**Fixed in migration 0044:**
- **`pipeline._EARLIER_CLEAN_ITEMS`** (absence detection, per unit) scanned the whole `work_units`
  table, every tenant's, for `unit_key = :u AND job_id <> :j`: no index led with `unit_key`.
  - Fix: a partial index `(tenant_id, unit_key)` on clean conversation-day units.
  - Test: 30,000 other units with their links and items → ≤ 20 rows read; the same query without
    the index reads ≥ 30,000.

**Found, then fixed the same day (user-approved): `items` for a tenant the statistics do not
know yet.** This hits every test tenant, and in production a new tenant's first large collections
until autoanalyze catches up at 2 % of the table.
- **FK checks** from `job_items` / `item_derivations` → `items (tenant_id, id)` ran on
  tenant-leading indexes with `id` as a filter.
- **The pipeline's per-batch lookups** (`idempotency_key = ANY`, versions, prior state) read the
  tenant's items so far: 38,180 and 27,937 rows in the sweep.
- **Fix:** migration 0045. `analyze_collected_items()` runs as a job's inserted items double;
  every ANALYZE function now takes no argument. ADR 0015 §24.20 "Update".

### A new tenant's first ~320k-item collection, before and after
`scripts/measure_new_tenant.py`:
- **Dataset:** 10 conversations × 5 days × 5,000 messages, pages of 200; one `Pipeline.run`.
- **Stack:** a FRESH test stack for each run, same laptop, nothing else running.
- **Statistics state:** `items`, `job_items` and `item_derivations` analyzed without the tenant,
  autovacuum held off on them for the run (a large shared table whose 2 % threshold one tenant's
  first collection does not reach).
- **"Before":** the pre-fix commit `acea195` in a worktree; "after": the fix.

| | Before (`acea195`) | After | Ratio |
|---|---|---|---|
| Wall time | **3,799 s** (63 min) | **790 s** (13.2 min) | 4.8x faster |
| Items inserted | 318,860 | 318,860 | |
| `items` tuples read | 401,519,881 | 2,827,369 | 142x fewer |
| Tuples read per item | 1,259 | 8.9 | |
| Per item | 11.9 ms | 2.5 ms | |
| ANALYZEs of `items` | 0 | 9 (1,024 ... 262,144) | |

"After" was measured on the first version of the fix, which counted inside the batch
transaction. The final version moves that one statement to its own transaction after the commit,
because the in-transaction version deadlocked (ADR 0015 §24.20). The ANALYZE schedule is the same
and the cost is one extra short statement per batch. It was re-measured on 2026-10-20 on the final
code (`d2b77e4`), on a fresh stack, with `--conversations 10 --days 5 --messages 5000`:

| | First version of the fix | Final code (`d2b77e4`) |
|---|---|---|
| Wall time | 790 s | 872.6 s |
| Items inserted | 318,860 | 318,860 |
| `items` tuples read | 2,827,369 | 2,825,474 |
| Tuples read per item | 8.9 | 8.9 |
| Per item | 2.5 ms | 2.7 ms |
| ANALYZEs of `items` | 9 | 9 |

The database work is the same to within 0.07 %, with the same nine refreshes. The wall time is 10 %
longer, but this run was not on a quiet machine (an editor session and a lint/typecheck ran
alongside), so the difference is within this laptop's noise. The extra statement per batch is not
visible in the tuples read. The integration mutation `pipeline_never_refreshes_items` (count after
commit removed) was re-run on the final code the same day: caught (the job's counter stays 0).

Progress of the "before" run, sampled every 2 min:
- **Collapsing rate:** items per 2 min fell from about 34,000 (minutes 2-4) to about 9,000 (minutes
  26-28).
- **Rising reads:** tuples read per 2 min rose from 8 M to 14 M. That is the quadratic in a real
  collection.
- **Larger collections:** the old code would degrade further. The new code stays at log2(n)
  refreshes and a flat per-item cost.

**Small and bounded (no action):** FK checks on `principals` (144 rows) and `directory_snapshots`
(55 rows) by sequential scan of tiny tables; the `slack_exports` FK check on `(tenant_id,
client_id)` with `id` as a filter (bounded by a tenant's exports).
