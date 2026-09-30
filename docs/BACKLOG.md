# Backlog

Ideas and later-phase work. Nothing here is in scope until promoted into a phase plan.

- S3 legal-hold flag in addition to per-matter retention (decided: backlog, 2026-09-30).
- Re-run WORM acceptance tests against real AWS S3 Object Lock before production.
- Mirror the pinned `pgsty/minio` / `pgsty/mc` images to our own registry (upstream MinIO stopped publishing images; the fork is third-party).
- **[required before production]** Least-privilege S3 identity for the app instead of root credentials (evidence writer needs PutObject*, GetObject*, no Delete*).
- AWS KMS implementation of `KmsClient`.
- Stronger tenant binding than a settable GUC (per-tenant DB roles or signed tenant context), see ADR 0007 residual risk.
- RFC 3161 trusted timestamping of custody anchors (TSA token stored next to each anchor) for court-grade proof of time.
- **[required before production]** Bucket policy denying `s3:PutObject` without `If-None-Match` on anchor/evidence prefixes, and denying `s3:DeleteObject` (delete markers) for the app identity.
- Anchor the exported package manifest to WORM at export time so the manifest hash itself is attested.
- **[with Phase 2 bulk export ingestion]** Custody lock-narrowing reorder (deferred FK, `append_batch` last), then re-measure. Per-unit chains stay deferred (docs/runs/2026-09-30-custody-contention.md).
