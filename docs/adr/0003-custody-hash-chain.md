# ADR 0003: Chain of custody as a hash chain with Merkle-rooted batches and WORM anchors

Status: Accepted (2026-09-30), implemented in M5 (`packages/custody`, migration 0002)

## Context
Every action on evidence must be recorded so the record itself is tamper-evident. Verification must catch
edits, deletions, reordering and truncation, including by someone with superuser access to Postgres.
A hash chain that lives only in the DB is not enough: a superuser can rewrite an event and recompute every
later hash, and the chain is internally valid again. Per-item events would mean 1M+ events per job.

## Decision

### Streams and events
- One chain per job (`stream_id = job_id`), plus one per tenant (`stream_id = tenant_id`) for non-job
  actions (connections, custodian merge/split).
- `event_hash = SHA-256( prev_hash_hex_ascii || canonical_json(fields) )`, where `fields` =
  `{tenant_id, stream_id, job_id, seq, event_type, actor, item_id, payload, created_at}` in RFC 8785
  canonical JSON. `created_at` uses the fixed `YYYY-MM-DDTHH:MM:SS.ffffffZ` form. Genesis
  `prev_hash` is 64 ASCII `0`s.
- Payloads must round-trip through JSONB exactly: floats and NUL characters are rejected, and ints must
  be in the I-JSON range.
- **Lifecycle actions are individual events:** job_started / retried / failed / finished / cancelled,
  unit_failed, activity_retried, report_generated, evidence_verified, connection_*, custodian_merged /
  split.
- **Batches:** one `items_collected` event per committed batch. Payload: `unit_key`,
  `page_evidence_id`, `page_sha256`, `item_count`, `merkle_root`. The items are linked through
  `job_items.custody_event_id` in the same transaction.

### Merkle tree (RFC 6962)
- `MTH({}) = SHA-256("")`, `leaf = SHA-256(0x00 || d)`, `node = SHA-256(0x01 || left || right)`.
  Split at k = largest power of two < n. This gives domain separation between leaves and nodes, and the
  last leaf is never duplicated: `[a,b,c]` ≠ `[a,b,c,c]`, so the CVE-2012-2459 class of ambiguity is
  closed.
- **Leaf data** is `bytes.fromhex(idempotency_key) || bytes.fromhex(content_hash)` (fixed 64 bytes).
- **Leaf order** is ascending `idempotency_key` (hex string order = byte order). Duplicate keys in a
  batch are an error.
- Tests: Certificate Transparency reference roots for n = 1…8; the empty tree; an independent
  bottom-up implementation at n = 0, 1, 2, 3, …, 1001, 4097 and under property tests; and no collisions
  among duplication variants.

### Concurrency
`custody_chain_heads` has one row per stream, locked `SELECT … FOR UPDATE` in the append
transaction, with `UNIQUE(stream_id, seq)`. A guard trigger allows only two kinds of update:
- **append:** `seq + 1`, with the anchor bookkeeping unchanged;
- **anchor bookkeeping:** seq and hash unchanged, `last_anchored_seq` monotonic and ≤ seq.

See `docs/runs/2026-09-30-custody-contention.md` for measurements.

### External anchoring (WORM)
- **When:**
  - at every lifecycle event;
  - whenever `seq - last_anchored_seq ≥ N` (`EDISC_CUSTODY_ANCHOR_EVERY_N_BATCHES`, default 8);
  - unconditionally at job finalize (the **seal**, whose key is recorded in
    `collection_jobs.seal_storage_key`).
- **How:** the append transaction sets `anchor_due` on the head. After commit, `anchor_if_due` writes
  the anchor. The flag survives crashes, so a missed anchor is written by the next caller.
- **Sweeper** (`edisc_custody.sweeper.sweep_anchors`, migration 0003): terminated or abandoned jobs have
  no next caller. A periodic sweeper anchors every stream that is `anchor_due`, or whose unanchored
  tail has been idle longer than `EDISC_CUSTODY_ANCHOR_SWEEP_IDLE_SECONDS` (default 600).
  - It finds them across tenants through `due_anchor_streams()`, a SECURITY DEFINER function owned by
    the NOLOGIN `edisc_sweeper` role. Its row policy exposes only overdue heads, and it returns ids
    only.
  - Anchoring itself goes through the normal tenant-scoped path.
  - Every stream is attempted, and failures are raised together (never swallowed).
  - It runs as a Temporal schedule (wired in M12) and must exist before Phase 3.
- **Object:** key `custody-anchors/<tenant>/<stream>/<seq:016d>.json`. The body is the deterministic
  canonical JSON `{format: "edisc-anchor/1", tenant_id, stream_id, seq, event_hash}`, so retries write
  byte-identical objects.
- **Write rules:** COMPLIANCE retention (the matter's `retention_until`, or 10 years for tenant
  streams, subject to the local/ci cap), `If-None-Match: *`, and a server-verified SHA-256. Each anchor
  is registered as an `evidence_objects` row of kind `anchor`.
- **Anchors are listed from the bucket, never from the DB.** Verification reads
  `ListObjectVersions`: every version of every anchor key must agree with the chain, and a delete
  marker is reported as an attempt to hide an anchor. A newer, shadowing version is caught because the
  locked original is still checked.

### Verification (`verify_chain`, and `edisc-verify` offline; the same `ChainVerifier` code)
Streaming, with memory O(anchors + one batch). It fails with a specific message when any of these is
violated:
- seq is gapless from 1;
- stream and tenant ids match;
- `prev_hash` links;
- recomputed event hashes match;
- each batch's `item_count` and Merkle root match its linked item rows;
- every anchor version agrees with the event at its seq;
- no anchor is beyond the head (truncation);
- no delete markers;
- the head row matches the last event;
- a finished job is sealed at its head.

### Append-only
- Row triggers reject UPDATE and DELETE, and a statement trigger rejects TRUNCATE, on `custody_events`,
  `items` and `job_items`. They fire for the owner too.
- The app role has INSERT/SELECT only on these tables, and TRUNCATE nowhere.

## Consequences
- + A superuser who rewrites the chain consistently is caught by any anchor at or after the first
  rewritten seq. This is tested at DB level and on exported packages.
- + ~5k events per 1M messages. Per-item provability is kept via Merkle roots.
- − **Exposure window:** events after the latest anchor are protected only by the DB until the next
  anchor: at most N−1 batch events, and never past a lifecycle event or finalize. For abandoned
  streams, the window is bounded by the sweeper's idle window plus its schedule interval.
- − Anchor time is attested only by S3 metadata. RFC 3161 trusted timestamps are in the backlog.
