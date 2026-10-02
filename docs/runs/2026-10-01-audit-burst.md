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

## After the fix (2026-10-02): coalesced anchors, one per due point (migration 0017)

**How it works now:**
- A writer must win an atomic claim on the head to anchor. Other writers skip instead of all anchoring.
- The claim targets the next **due point**: the next interval boundary (every N events) or the pending
  lifecycle event, never the moving head.
- The claimer drains every due point before returning. A claim older than
  `EDISC_CUSTODY_ANCHOR_CLAIM_TIMEOUT_SECONDS` (60) is taken over; the sweeper does this for a claimer killed
  mid-anchor. An ordinary failure releases the claim immediately.

| Concurrency | Mode | req/s before fix | req/s after fix |
|---:|---|---:|---:|
| 50 | audited (production) | 97 | **124** |
| 50 | audited_shared_stream_no_anchor | 134 | 149 |
| 200 | audited (production) | 117 | **166** |
| 200 | audited_shared_stream_no_anchor | 161 | 195 |

(Absolute numbers drift between runs on this laptop; compare within a column.)

**Results:**
- Anchors: **116 for the 928 events the audited mode anchored = one per 8 events exactly**, down from
  360–454 for 771 before.
- The remaining unanchored tail comes from the measurement modes that deliberately skip anchoring; the
  sweeper covers it.
- The remaining audited cost (about 15%) is the S3 PUT of one anchor every 8 events, plus one claim
  attempt per request.
- Per-matter or per-day audit streams stay unnecessary.

**Tests** (`tests/integration/custody/test_anchor_claims.py`):
- 200 concurrent writers produce one anchor per due point. The gap between anchors is never more than N,
  the lifecycle event is itself anchored, and the quiescent tail is under N. Mutation-checked: without the
  claim, 46 anchors were written for 200 events.
- A fresh claim blocks the sweeper; a stale claim is taken over.
- An ordinary failure releases the claim.
- The 50k SIGKILL acceptance run passes with the new anchoring (81 s).
