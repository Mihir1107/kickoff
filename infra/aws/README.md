# AWS bucket configuration (production)

Applied by infrastructure provisioning, never by application code.

| Bucket | Settings |
|---|---|
| evidence | Object Lock enabled at creation, default COMPLIANCE retention (small safety net), versioning on (implied), SSE-KMS, `evidence-bucket-lifecycle.json` (aborts incomplete multipart uploads after 1 day; **no expiration rules**: objects expire only when their retention lapses and a separate destruction process deletes them) |
| staging | No Object Lock, SSE-KMS, `staging-bucket-lifecycle.json` (under `t/`: objects and incomplete uploads removed after 1 day; uploaded Slack exports under `exports/`: after 7 days, matching `EDISC_EXPORT_UPLOAD_TTL_DAYS`) |

Required before production (see docs/BACKLOG.md): a bucket policy denying `s3:PutObject` without
`If-None-Match` and denying `s3:DeleteObject` for the app identity on the evidence bucket, and a
least-privilege app identity.
