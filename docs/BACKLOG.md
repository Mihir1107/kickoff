# Backlog

Ideas and later-phase work. Nothing here is in scope until promoted into a phase plan.

- S3 legal-hold flag in addition to per-matter retention (decided: backlog, 2026-09-30).
- Re-run WORM acceptance tests against real AWS S3 Object Lock before production.
- Mirror the pinned `pgsty/minio` / `pgsty/mc` images to our own registry (upstream MinIO stopped publishing images; the fork is third-party).
- Least-privilege MinIO/S3 user for the app instead of root credentials (evidence writer needs PutObject*, GetObject*, no Delete*).
- AWS KMS implementation of `KmsClient`.
- Stronger tenant binding than a settable GUC (per-tenant DB roles or signed tenant context), see ADR 0007 residual risk.
- RFC 3161 trusted timestamping of custody anchors (TSA token stored next to each anchor) for court-grade proof of time.
- Bucket policy denying `s3:PutObject` without `If-None-Match` on anchor/evidence prefixes, and denying `s3:DeleteObject` (delete markers) for all app credentials.
- Anchor the exported package manifest to WORM at export time so the manifest hash itself is attested.
- Custody contention: if M14 shows the shared job chain limits throughput, first apply the deferred-FK "append last" change, then consider per-unit chains rolled up into the job chain (docs/runs/2026-09-30-custody-contention.md). Needs approval.
- Periodic anchor sweeper (Temporal schedule) that anchors any stream with `anchor_due` set, bounding the window after a crash.
