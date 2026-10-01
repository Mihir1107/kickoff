# 2026-10-01: tenant audit chain under a burst of evidence downloads (measurement; nothing changed)

```
make test-env-up
EDISC_ENV_FILE=.env.test uv run python scripts/measure_audit_burst.py --concurrency 50 200
```

**Setup:**
- N concurrent `GET /v1/evidence/{id}/content?purpose=preview` through the real API: in-process ASGI,
  real Postgres/MinIO/Redis, the production DB pool (10 + 10).
- 1 tenant, 3 rounds per mode, page objects of about 45 KB.
- M3 laptop, shared and noisy, so compare modes within one run.
- Every read appends `audit.evidence_content_read` to the tenant's audit chain (custody stream = tenant id)
  before streaming, then calls `anchor_if_due` after commit.

**Modes:**
- `audited`: production behaviour.
- `audited_shared_stream_no_anchor`: the same append, with the post-commit anchor call skipped.
- `audited_unshared_streams`: each request appends to its own stream, so there is no shared chain head.
- `baseline_no_audit`: no audit at all, i.e. the cost of the download itself.

## Results

| Concurrency | Mode | req/s | p50 | p95 | append p50 / p95 / max |
|---:|---|---:|---:|---:|---|
| 50 | baseline_no_audit | 138 | 316 ms | 361 ms | |
| 50 | audited_unshared_streams | 138 | 320 ms | 352 ms | 10 / 21 / 28 ms |
| 50 | audited_shared_stream_no_anchor | 134 | 327 ms | 401 ms | 47 / 78 / 88 ms |
| 50 | **audited (production)** | **97** | **476 ms** | **519 ms** | 43 / 91 / 131 ms |
| 200 | baseline_no_audit | 204 | 782 ms | 1040 ms | |
| 200 | audited_unshared_streams | 171 | 902 ms | 1126 ms | 10 / 17 / 69 ms |
| 200 | audited_shared_stream_no_anchor | 161 | 915 ms | 1172 ms | 49 / 104 / 134 ms |
| 200 | **audited (production)** | **117** | **1418 ms** | **1667 ms** | 27 / 91 / 213 ms |

## Findings

1. **The shared chain head is not the bottleneck.**
   - Contention on the tenant's head row raises the append itself from about 10 ms to about 48 ms (p50).
   - But requests overlap that wait with other I/O. With anchoring skipped, the shared stream costs 3% of
     throughput at 50 concurrent requests and 6% at 200, compared with fully unshared streams.
2. **Anchoring under concurrency is the bottleneck, and it is a defect (anchor storm).**
   - Production mode loses about 30% of throughput and adds about 160 ms (50) to 500 ms (200) at p50.
   - Cause: once `anchor_due` is set, every concurrent request reads it, and each anchors whatever head
     sequence it saw: S3 PUT + 3 transactions. Under a burst the head keeps advancing, so `anchor_due` is
     never cleared (it clears only when `last_seq` equals the anchored seq).
   - Counted after the runs: **360–454 anchors for 771 audit events**, where the design intends one per 8
     events (about 96). The same race exists on job streams with 8 units in flight, at a lower rate.
   - Anchors are WORM objects, so the storm also creates permanent objects.
3. **A test-only finding, fixed:** the dev IdP re-parsed its RSA key on every request (about 75 ms of CPU on
   the event loop). That made the first measurement about 12 req/s for every mode, and it would have hidden
   all of the above. The key is now cached per file version. Real IdPs were never affected: JWKS keys are
   cached.

## Proposal (not implemented; for decision)
**A. Coalesce anchors** (recommended; fixes the cause for audit and job streams; no ADR change):
- Claim an anchor atomically. Add `anchoring_seq` to `custody_chain_heads`, and anchor only if
  `UPDATE ... SET anchoring_seq = :seq WHERE anchoring_seq IS NULL OR anchoring_seq < last_anchored_seq`
  succeeds. Concurrent requests then skip instead of piling on.
- After the anchor completes, clear the claim and keep `anchor_due` set while the head is more than N events
  past the new anchor. The next writer (or `sweep_anchors`) anchors again.
- Abandoned claims are released after a timeout by the existing anchor sweeper.
- Expected: production mode within a few percent of `audited_shared_stream_no_anchor`, and about one anchor
  per N events.
- Tests: the burst above with an assertion on the anchor count (≤ events / N + concurrency), plus
  `verify_chain` on the tenant stream.

**B. Per-matter (or per-day) audit streams** (what you asked me to consider). It is not justified by these
numbers:
- it removes only the head-contention part (≤6% here);
- it fragments the tenant audit trail into many chains that each need sealing and anchoring;
- it makes "show me everything this user did" a multi-chain query.

Revisit it if A is in and a real burst profile shows head contention above about 15%.

The audit append (about 10 ms unshared) is otherwise cheap next to the download it records.
