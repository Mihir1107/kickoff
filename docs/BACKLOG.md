# Backlog

Ideas and later-phase work. Nothing here is in scope until promoted into a phase plan.

- Postgres row-level security for tenant isolation (pending decision, plan Q6).
- S3 legal-hold flag in addition to per-matter retention (plan Q5).
- Re-run WORM acceptance tests against real AWS S3 Object Lock before production.
- Mirror the pinned `pgsty/minio` / `pgsty/mc` images to our own registry (upstream MinIO stopped publishing images; the fork is third-party).
- Least-privilege MinIO/S3 user for the app instead of root credentials (evidence writer needs PutObject*, GetObject*, no Delete*).
- AWS KMS implementation of `KmsClient`.
