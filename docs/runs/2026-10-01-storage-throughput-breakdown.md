# 2026-10-01: storage and throughput breakdown (10k messages)

Measurement only. Nothing has been changed yet; the options at the end need a decision.

```
make test-env-up        # fresh ephemeral stack, so the deltas belong to this run alone
EDISC_ENV_FILE=.env.test uv run python scripts/measure_breakdown.py --out breakdown.json
# second shape, one big unit:
EDISC_ENV_FILE=.env.test uv run python scripts/measure_breakdown.py \
    --conversations 1 --days 1 --messages-per-unit 10000 --out breakdown-bigunit.json
```

- **Main shape:** 5 conversations x 4 days x 500 messages = 10,000 messages, page size 200.
- **How each part was measured:**
  - Storage (part A) went through Temporal with 3 worker processes and no kills. It compares
    before/after snapshots of Postgres (heap, TOAST and each index separately, WAL), the `temporal` and
    `temporal_visibility` databases, MinIO (logical bytes per object kind, plus on-disk bytes from `du`
    inside the container) and workflow history sizes.
  - Throughput (part B) ran in process without Temporal on a second tenant. It wraps each stage with a
    timer.
- **Machine:** M3 laptop, Docker VM with 8 CPUs and 8 GB.

Item mix (counted on the one-big-unit run): 10k messages became ~13,300 items (10,000 messages, 2,510
events such as reactions, edits and observations, and 811 file items). Files are deduplicated: in the main
run, 1,137 file references gave 306 unique content-addressed objects.

## Storage: bytes per message

| Component | Bytes (10k run) | B / message | Share |
|---|---:|---:|---:|
| **MinIO page objects** (raw API pages, exact bytes) | 36,337,068 | 3,634 | 53% |
| MinIO per-object overhead (disk minus logical, 403 objects: xl.meta, block rounding) | 4,013,334 | 401 | 6% |
| **MinIO attachments (dummy file bytes)** | 622,495 | 62 | 0.9% |
| MinIO custody anchors (16 objects) | 3,487 | 0.3 | 0% |
| Postgres `items` heap (~546 B per item row) | 7,421,952 | 742 | 11% |
| Postgres `items` indexes (6 indexes) | 7,397,376 | 740 | 11% |
| Postgres `item_derivations` heap (derived JSON ~340 B) + pk | 6,955,008 | 696 | 10% |
| Postgres `job_items` heap + 3 indexes | 2,899,968 | 290 | 4% |
| Postgres `evidence_objects`, `custody_events`, other | ~470,000 | 47 | 1% |
| Temporal (`temporal` + `temporal_visibility` DBs; histories 167 KB, 22 runs) | 2,498,560 | 250 | 4% |
| **Total at rest** | **~68.6 MB** | **~6,860** | |
| Postgres WAL generated (transient, recycled at checkpoints; `wal_compression=off`) | 39,007,752 | 3,901 | (not at rest) |

**Attachments vs per-message overhead.** Dummy attachments are 62 B/message, under 1% of the total; the dummy
dataset has small files (200 to 4,000 bytes). Per-message overhead without attachments is about **6.8 KB at
rest**. Real attachments will dominate in production and scale with the source, not with us.

`items` indexes, in detail (bytes for 10k messages):

| Index | Bytes | Note |
|---|---:|---|
| `uq_items_tenant_id_idempotency_key` | 1,990,656 | key is 64 chars of text |
| `uq_items_tenant_id_source_source_item_id_version` | 1,859,584 | |
| `ix_items_tenant_id_source_source_item_id` | 1,728,512 | **a prefix of the unique index above: redundant** |
| `ix_items_tenant_id_source_sent_at` | 753,664 | used by the absence/observed queries |
| `uq_items_tenant_id_id` | 647,168 | target of composite tenant FKs |
| `pk_items` | 417,792 | |

The one-big-unit shape (1 x 1 x 10,000) stores about the same per message (Postgres 2.65 KB, MinIO 4.9 KB,
because more thread-context pages are written).

**Correction to the 1M estimate** (docs/runs/2026-10-01-resume-soak.md): the 26 KB/message figure was wrong.
That stack's volumes also held the earlier 50k run and the integration tests, and up to 1 GB of WAL
(`max_wal_size`). The clean measurement is **~6.9 KB/message at rest plus up to ~1 GB of WAL**, so 1M
messages needs about 7 GB of volumes. `resume_soak.py`'s disk guard still uses 26 KB/message, so it asks
for far more space than needed. Updating it is in the options below.

## Throughput: where the time goes

In process, without Temporal: 10,000 messages in 54.8 s (183 msg/s), 81 pages, 20 units.

| Stage | Total s | Calls | Per page (ms) | Share of wall |
|---|---:|---:|---:|---:|
| **File evidence** (`_files`: 1,137 file writes, 14 per page, 28 ms each) | 32.0 | 1,137 | 395 | **58%** |
| Unit finalize (re-reads the last page from S3, absence detection) | 7.4 | 21 | 352 per unit | 13% |
| Persist + links + custody (`_link_batch`; `persist` alone 4.1 s) | 5.6 | 81 | 70 | 10% |
| `load_prior` (prior versions, hints, observations) | 3.8 | 121 | 47 | 7% |
| Normalize (pure CPU) | 1.8 | 81 | 23 | 3% |
| Source fetch (dummy dataset generation, test-only CPU) | 1.5 | 80 | 19 | 3% |
| Page evidence (`write_page`) | 1.1 | 81 | 13 | 2% |
| Custody anchors (`anchor_if_due`) | 0.3 | 103 | 3 | <1% |
| Rest (unit lock, checkpoint update, commit, job start/finalize) | ~1.3 | | | 2% |

- **Files dominate.** Every file write streams to the staging bucket, hashes, takes the per-content
  advisory lock, server-side copies into the content-addressed key, verifies and deletes staging. That is
  several round trips per file, even for the 73% that turned out to be duplicates.
- **Through Temporal** with 3 worker processes and 8 units in flight: 20.9 s (478 msg/s, 2.6x in-process).
- **Big unit** (1 x 1 x 10,000): files are 80% of batch time. The Temporal run took 87.7 s because one
  conversation-day is one serial unit. The unit of work bounds parallelism (ADR 0005).

## Options to reduce

Storage:

- **S1. Compress page objects; the evidence hash stays over the original bytes.**
  - Store pages zstd/gzip-compressed with metadata (`content-encoding`, original sha256 and size).
    Verification and export decompress while streaming. The evidence hash and the items' `raw_hash` are
    unchanged.
  - Dummy pages compress 32x (zlib-6) or 63x (lzma-6), but that is unrealistically repetitive. Real Slack
    JSON is typically 5 to 10x and has to be measured on real exports first.
  - Impact: up to ~50% of at-rest bytes here, likely 30 to 45% on real data.
  - Cost: an ADR 0002 amendment (the stored object is no longer byte-identical to the response; the
    original bytes are reconstructible and hash-checked), and decompression in `verify`, the offline
    verifier and exports.
- **S2. Larger pages per request, not merged objects.**
  - Raise the page size where the source allows it (Slack history accepts up to 999). That means fewer
    objects (the per-object overhead is 6%), fewer requests against Slack's per-request rate tiers, and
    fewer custody events.
  - Merging several responses into one object would break "one object = one exact response". I recommend
    against that.
- **S3. Index review on `items`.**
  - Drop `ix_items_tenant_id_source_source_item_id`; it is a prefix of the unique `(tenant, source,
    source_item_id, version)` index (-7% of Postgres bytes, and one less index on every insert).
  - Store the idempotency key as a 32-byte sha256 digest instead of 64 characters of text (about -0.5 MB
    per 10k in index plus heap). Both need a migration; the first is trivial.
- **S4. Slimmer `item_derivations`.** At ~340 B per row, fields that duplicate `items` columns could be
  dropped from the derived JSON. This needs a careful look at what the verifier and export read.
- **S5. `wal_compression = lz4`** (Postgres 16). Full-page writes dominate the 3.9 KB/message of WAL.
  This is a cheap config change; it doesn't change bytes at rest, but it reduces WAL disk, backup and
  replication volume.

Throughput:

- **T1. Small-file fast path.**
  - For files under a cap (e.g. 8 MB, bounded memory): read them into memory, hash, HEAD the
    content-addressed key, and if it exists record a dedup with no upload. Otherwise do a single
    `If-None-Match` PUT straight to the WORM key under the advisory lock. No staging, no server-side copy.
  - Large files keep the staging path (ADR 0002 amendment). Expected: 28 ms per file down to roughly
    5 to 10 ms, and near-zero for duplicates.
- **T2. Bounded concurrent file writes within a page** (e.g. 4 to 8), with per-file error classification.
  Together with T1, file time per page should drop from ~395 ms to well under 100 ms.
- **T3. Unit finalize without re-reading S3.**
  - Keep what absence detection needs from the last history page in the database at batch time (or
    derive it from the batch's items) instead of downloading the page again: about -350 ms per unit.
  - The page stays the evidence; only the in-memory copy goes away.
- **T4. `load_prior` and `persist` (~120 ms per page together).**
  - Check with EXPLAIN. Likely wins: an index on `item_derivations (item_id, created_at DESC)` for the
    LATERAL lookup, and S3 above.
  - The per-row `guard_job_open` trigger (backlog item) is the next cost.
- **T5. Parallelism inside a hot conversation-day** (time-sliced sub-units). This is a larger ADR 0005
  change, only worth it if real tenants have very busy single channels. Defer.
- **Disk guard correction:** set `resume_soak.py` to ~8 KB/message plus 1 GB of WAL headroom. The 1M run
  then fits in about 25 GB free.

Recommended order, smallest risk first:

1. S3 (drop the redundant index), S5 (WAL compression), T3 (finalize without S3 re-read) and the disk guard
   correction. Small and local.
2. T1 + T2 (the file path), with an ADR 0002 amendment for the small-file path. This is the biggest
   throughput win.
3. S1 (page compression), only after measuring on real Slack exports. It needs an ADR 0002 amendment and
   verifier changes.
4. S2 per connector (Slack page size) in Phase 2. S4 after the export format is settled. T5 deferred.

## Results after options 1 and 2 (same day)

**Implemented:**
- **Option 1:** migration 0014, `wal_compression=lz4`, unit finalize without re-reading the page, and the
  soak disk check.
- **Option 2:** the small-file direct path, a dedup pre-check without the content lock, and bounded
  parallel file writes per page (ADR 0002 amendment).
- Option 3 (compression) is deferred until there is real Slack data.

### Storage (deterministic; fresh stack, 10k messages)

| | Before | After option 1 |
|---|---:|---:|
| Postgres `edisc` growth | 25,591,808 | 23,781,376 (-7%) |
| `items` indexes | 7,397,376 | 5,619,712 (-24%) |
| WAL generated | 39,007,752 | 36,647,432 (-6%; lz4 helps less than expected here) |
| MinIO on disk | 40,976,384 | 40,984,576 (unchanged; option 2 changes how files are written, not what is stored) |

### Finalize: the real cause was query plans, not the S3 re-read
- Skipping the page re-read alone left unit finalize at ~300 ms per unit.
- Per-statement timing found the cause: the absence/observed queries. With table statistics still stale
  mid-load (no ANALYZE yet), the planner drove them from the `(tenant, source, sent_at)` index and scanned
  a whole day's items for every link: 1,009,753 rows filtered, 339 ms. It should have looked up the
  unit's ~650 links by primary key.
- **Fix:**
  - the queries select linked items by id only (`id = ANY(ARRAY(...job_items...))`), and the
    day/message filter moved into Python;
  - autovacuum analyzes the bulk tables after 2% change.
- Result: unit finalize **6.9 s → 0.33 s** for 21 units.

### Throughput, option 2 (paired runs, fresh stack each)
- **Before** = the same code with `EDISC_EVIDENCE_SMALL_FILE_MAX_BYTES=0` and
  `EDISC_EVIDENCE_FILE_CONCURRENCY=1`, i.e. the old file path. **After** = the defaults (8 MiB, 4).
- The machine was shared and noisy during these runs: absolute times vary up to 2x between rounds.
  Compare within a round.

| Round | Path | In-process wall | msg/s | File time | Batch time | Through Temporal (3 workers) |
|---|---|---:|---:|---:|---:|---:|
| 1 | before | 72.9 s | 137 | 42.8 s | 67.5 s | 36.3 s |
| 1 | after | 30.1 s | 333 | 10.4 s | 27.0 s | 47.6 s |
| 2 | before | 61.3 s | 163 | 32.8 s | 57.3 s | 27.5 s |
| 2 | after | 39.7 s | 252 | 13.9 s | 35.9 s | 27.2 s |

- **File time is down 58 to 76%. Per-process throughput is up 1.5 to 2.4x.**
  - Duplicates (73% of file writes) cost one source read and one registry query, with no lock and no
    upload.
  - Small files are one locked PUT, with no staging object and no server-side copy.
- **The Temporal soak did not get faster** (27 to 48 s, all within noise).
  - With 8 units in flight across 3 processes, file latency was already overlapped.
  - That run is bound by worker CPU (normalization, canonical JSON, dummy generation) on a shared 8-core
    laptop, plus Temporal round trips.
  - Next lever, if needed: more worker processes or hosts, not the file path.
- **New finding, not changed:** every dedup hit calls `PutObjectRetention`, because the rolling target
  `now + window` moves every second (831 registry updates and S3 calls in this run).
  - Proposal: extend only when the target exceeds the current retain-until by more than a slack
    (e.g. min(1 day, 10% of the window)).
  - Concurrent extensions are already safe: a refused shortening that another writer already exceeded
    counts as success (tested).

### 1M soak
- Not run locally. Free disk was 14 to 15 GB, varying with system swap. The corrected check needs 20.5 GB:
  ~10.5 GB projected (7 KB x 1.5 x 1M, plus 2 GB of WAL) and 8 GB left after the run.
- It stays in the backlog for a cloud VM, as decided.
