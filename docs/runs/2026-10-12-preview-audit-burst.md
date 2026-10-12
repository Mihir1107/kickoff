# 2026-10-12: the cost of one forced anchor per preview page view (ADR 0018 §10, D14)

**Command** (fresh ephemeral test stack, `make test-env-up`; the laptop also ran other work, so read
the paired differences, not the absolutes):

    EDISC_ENV_FILE=.env.test uv run python scripts/measure_audit_burst.py --target preview \
        --concurrency 1 10 50 --rounds 3

**Setup:** one tenant, one sealed dummy job (4 conversations x 3 days x 60 messages, page size 20),
requests cycle over its 12 conversation-days (`GET /v1/jobs/{id}/preview`, about 60 events per page),
real API in-process (ASGI), real Postgres/MinIO/Redis, production pool size. Modes: `audited`
(production: `audit.preview_read` appended to the tenant chain + `audit.anchor_now`, a forced WORM
anchor, before the first byte), `audited_no_anchor` (the anchor replaced by a no-op), `audited_unshared_streams`
(each append on its own stream, no anchor), `baseline_no_audit`.

| mode | p50 @1 | p50 @10 | p95 @10 | p50 @50 | p95 @50 | req/s @50 |
|---|---|---|---|---|---|---|
| audited (production) | 118 ms | 653 ms | 750 ms | 3,953 ms | 4,110 ms | 12.3 |
| audited, no anchor | 95 ms | 507 ms | 516 ms | 3,285 ms | 3,542 ms | 14.2 |
| audited, unshared streams | 95 ms | 496 ms | 513 ms | 3,023 ms | 3,496 ms | 14.6 |
| no audit (baseline) | 94 ms | 513 ms | 693 ms | 2,763 ms | 3,373 ms | 15.1 |

**Reading.** A page view costs about 95 ms of loading (pinned reads re-verified) and rendering in this
one-process setup; requests share one event loop and the renderer's thread pool, so latency grows
with concurrency in every mode. The forced anchor adds about 24 ms to a single view (one WORM
PutObject plus the claim) and about 20 % at 50 concurrent views (3.95 s vs 3.29 s p50; 12.3 vs 14.2
pages/s). Contention on the tenant chain head is small next to it (unshared vs shared, no anchor).

**Decision (D14):** keep one forced anchor per page view. The group-commit fallback of §10 is not
needed at these rates (a reviewer pages through a day by hand; 50 concurrent views of one tenant is
far above the expected load). Re-measure on the cloud VM with production S3 latencies before
production (BACKLOG, with the 1M soak).

```json
{
  "audited@1": {
    "requests": 3,
    "p50_ms": 118.4,
    "p95_ms": 132.1,
    "max_ms": 132.1,
    "wall_s_per_burst": 0.12,
    "req_per_s": 8.4,
    "audit_append_p50_ms": 1.5,
    "audit_append_p95_ms": 1.5,
    "audit_append_max_ms": 1.5
  },
  "audited_no_anchor@1": {
    "requests": 3,
    "p50_ms": 94.6,
    "p95_ms": 96.8,
    "max_ms": 96.8,
    "wall_s_per_burst": 0.09,
    "req_per_s": 10.6,
    "audit_append_p50_ms": 1.3,
    "audit_append_p95_ms": 1.4,
    "audit_append_max_ms": 1.4
  },
  "audited_unshared_streams@1": {
    "requests": 3,
    "p50_ms": 95.2,
    "p95_ms": 95.3,
    "max_ms": 95.3,
    "wall_s_per_burst": 0.1,
    "req_per_s": 10.5,
    "audit_append_p50_ms": 1.5,
    "audit_append_p95_ms": 1.5,
    "audit_append_max_ms": 1.5
  },
  "baseline_no_audit@1": {
    "requests": 3,
    "p50_ms": 93.5,
    "p95_ms": 94.2,
    "max_ms": 94.2,
    "wall_s_per_burst": 0.09,
    "req_per_s": 10.7
  },
  "audited@10": {
    "requests": 30,
    "p50_ms": 653.1,
    "p95_ms": 749.7,
    "max_ms": 750.0,
    "wall_s_per_burst": 0.67,
    "req_per_s": 14.9,
    "audit_append_p50_ms": 14.4,
    "audit_append_p95_ms": 56.1,
    "audit_append_max_ms": 81.6
  },
  "audited_no_anchor@10": {
    "requests": 30,
    "p50_ms": 506.7,
    "p95_ms": 516.3,
    "max_ms": 516.8,
    "wall_s_per_burst": 0.52,
    "req_per_s": 19.3,
    "audit_append_p50_ms": 9.9,
    "audit_append_p95_ms": 54.5,
    "audit_append_max_ms": 88.6
  },
  "audited_unshared_streams@10": {
    "requests": 30,
    "p50_ms": 495.7,
    "p95_ms": 512.5,
    "max_ms": 513.4,
    "wall_s_per_burst": 0.51,
    "req_per_s": 19.6,
    "audit_append_p50_ms": 13.0,
    "audit_append_p95_ms": 46.9,
    "audit_append_max_ms": 54.6
  },
  "baseline_no_audit@10": {
    "requests": 30,
    "p50_ms": 512.5,
    "p95_ms": 692.7,
    "max_ms": 692.8,
    "wall_s_per_burst": 0.56,
    "req_per_s": 18.0
  },
  "audited@50": {
    "requests": 150,
    "p50_ms": 3953.0,
    "p95_ms": 4109.6,
    "max_ms": 4115.3,
    "wall_s_per_burst": 4.05,
    "req_per_s": 12.3,
    "audit_append_p50_ms": 136.3,
    "audit_append_p95_ms": 326.2,
    "audit_append_max_ms": 393.7
  },
  "audited_no_anchor@50": {
    "requests": 150,
    "p50_ms": 3285.2,
    "p95_ms": 3541.7,
    "max_ms": 3550.5,
    "wall_s_per_burst": 3.52,
    "req_per_s": 14.2,
    "audit_append_p50_ms": 178.3,
    "audit_append_p95_ms": 618.7,
    "audit_append_max_ms": 932.7
  },
  "audited_unshared_streams@50": {
    "requests": 150,
    "p50_ms": 3023.4,
    "p95_ms": 3495.5,
    "max_ms": 3607.1,
    "wall_s_per_burst": 3.41,
    "req_per_s": 14.6,
    "audit_append_p50_ms": 57.5,
    "audit_append_p95_ms": 177.6,
    "audit_append_max_ms": 230.5
  },
  "baseline_no_audit@50": {
    "requests": 150,
    "p50_ms": 2762.5,
    "p95_ms": 3372.8,
    "max_ms": 3503.2,
    "wall_s_per_burst": 3.3,
    "req_per_s": 15.1
  }
}```
