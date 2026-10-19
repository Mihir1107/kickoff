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

**Found, NOT fixed (needs a decision, HANDOFF "Next"):** the same stale-statistics pattern on
`items` for a tenant the statistics do not know yet (every test tenant; in production, a new
tenant's first large collections until autoanalyze catches up at 2 % of the table):
- **FK checks** from `job_items` / `item_derivations` → `items (tenant_id, id)`: 40 of 5,366 sampled
  checks were GENERIC plans on `uq_items_tenant_id_source_source_item_id_version` or
  `ix_items_tenant_id_source_sent_at`, with `id` as a filter, reading the tenant's items so far (up
  to 372 per check here).
- **The pipeline's per-batch lookups:**
  - `idempotency_key = ANY($2)` read 38,180 rows through `ix_items_tenant_id_source_sent_at`
    instead of `uq_items_tenant_id_idempotency_key`;
  - the version lookup (`source_item_id = ANY($3)`) read 27,937 with the array as a filter.
- **Effect:** each batch rescans the tenant's items so far, which is quadratic in a new tenant's
  first collection.
- **The difficulty:** every competing index leads with `tenant_id`, and no per-transaction setting
  chooses between indexes.

**Small and bounded (no action):** FK checks on `principals` (144 rows) and `directory_snapshots`
(55 rows) by sequential scan of tiny tables; the `slack_exports` FK check on `(tenant_id,
client_id)` with `id` as a filter (bounded by a tenant's exports).
