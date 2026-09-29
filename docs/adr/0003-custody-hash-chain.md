# ADR 0003: Chain of custody as a hash chain with Merkle-rooted batches

Status: Accepted (2026-09-30)

## Context
Every action on evidence must be recorded so the record itself is tamper-evident, and verification must
catch edits, deletions, reordering and truncation. Per-item events would mean 1M+ events per job and
heavy lock contention.

## Decision
- **Streams:** one chain per job (`stream_id = job_id`) plus one per tenant for non-job actions
  (connection created/validated, custodian merge/split, matter changes).
- **Event hash:** `event_hash = sha256(prev_hash || canonical_json(event))`, where `canonical_json` is
  RFC 8785 over `{tenant_id, stream_id, job_id, seq, event_type, actor, item_id, payload, created_at}`.
  Genesis `prev_hash` = 32 zero bytes (hex).
- **Batches:** one `items_collected` event per committed batch. Payload: unit_key, page evidence object
  id + sha256, item count, and a **Merkle root over `(idempotency_key, content_hash)`** of every item the
  batch observed (inserted or already present). Leaves: `sha256(0x00 || idempotency_key || 0x1F ||
  content_hash)` sorted by idempotency_key; nodes: `sha256(0x01 || left || right)`; an odd node is
  promoted unchanged (RFC 6962 style). Empty batch root = sha256 of empty string.
- **Lifecycle events are individual:** job_started, unit_started, activity_retried, unit_failed,
  job_failed, job_finished (with final status and reconciliation summary), report_generated,
  evidence_verified, connection_*, custodian_merged/split.
- **Concurrency:** `custody_chain_heads` row per stream locked `SELECT … FOR UPDATE` in the same
  transaction as the append; `UNIQUE(stream_id, seq)`. Gapless, safe with many writers.
- **Append-only:** triggers reject UPDATE, DELETE and TRUNCATE on `custody_events`; the app role has
  INSERT/SELECT only.
- **Seal:** on job finalize, `{stream_id, last_seq, last_hash}` is written as a WORM object and its key
  recorded on the job. This detects tail truncation, which a chain alone cannot.
- **`verify_chain(stream_id)`** checks: seq gapless from 1; each prev_hash links; each event_hash
  recomputes; **each batch Merkle root recomputes from `job_items` ⋈ `items`** (linked via
  `job_items.custody_event_id`); head matches the WORM seal (if sealed). Any mismatch fails with the
  first offending seq.

## Consequences
- + ~5k chain events per 1M messages instead of 1M; per-item provability retained via Merkle root.
- + Tampering with an item row (hash, key) is detected through its batch root.
- − Verification cost is proportional to items (acceptable; streaming, run on demand and at finalize).
