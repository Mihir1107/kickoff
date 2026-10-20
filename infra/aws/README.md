# AWS bucket configuration (production)

Applied by infrastructure provisioning, never by application code.

| Bucket | Settings |
|---|---|
| evidence | Object Lock enabled at creation, default COMPLIANCE retention (small safety net), versioning on (implied), SSE-KMS, `evidence-bucket-lifecycle.json` (aborts incomplete multipart uploads after 1 day; **no expiration rules**: objects expire only when their retention lapses and a separate destruction process deletes them) |
| staging | No Object Lock, SSE-KMS, `staging-bucket-lifecycle.json` (under `t/`: objects and incomplete uploads removed after 1 day; uploaded Slack exports under `exports/`: after 7 days, matching `EDISC_EXPORT_UPLOAD_TTL_DAYS`) |

**Storage identities (M18 §3, ADR 0019).** Three roles: app (API and every collection/export/
render/report worker), hold (the holds worker), destroy (the destruction worker). Their identity
policies are `infra/minio/policies/*.json` (the same JSON works as IAM policies; replace the
`@EVIDENCE@` / `@STAGING@` bucket names), each with explicit Deny statements for what the role must
never do. Root (the account) is never used by a service.

**Bucket policies, the second layer on AWS** (`evidence-bucket-policy.json`,
`staging-bucket-policy.json`; replace `@EVIDENCE@`, `@STAGING@` and the `@*_ROLE_ARN@` role ARNs):
no delete markers, version deletes only by the destroy role, hold flags only by the hold role, no
governance bypass, no bucket configuration, writes only under the evidence prefixes; staging for
the app role only. **MinIO does not evaluate bucket policies for IAM users** (M18 step 0 spike), so
locally the explicit denies in the identity policies are the second layer and these files are
unused. *(confirm on AWS: every statement refused for the other roles, `aws:PrincipalArn` of an
assumed role.)*

Still required before production (docs/BACKLOG.md, cloud): a statement denying `s3:PutObject`
without `If-None-Match` (`s3:if-none-match`; MinIO rejects the condition key).
