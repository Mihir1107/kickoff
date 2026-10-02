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
- **[required before production]** Matter-level retention extension job: while a matter is active, extend COMPLIANCE retain-until of its objects (and anchors) toward `now + EDISC_EVIDENCE_RETENTION_WINDOW_DAYS`; stop at matter close so objects expire on schedule (ADR 0002). Needs a matter status/close workflow and custody events for extensions.
- Destruction workflow after matter close + retention lapse (verified deletion of every version, custody events, certificate of destruction).
- AWS optimization for files > 5 GB: verify UploadPartCopy destinations by comparing the SHA-256 composite against our own per-range composite instead of a full re-read (ADR 0002).
- **[required before production]** Staging bucket: encrypted at rest (SSE-KMS), least-privilege access (the app may write/read/delete only its own staging prefix; nothing else may read it), alerting on objects older than the 1-day expiry.
- **[required before production]** On AWS, verify whether CopyObject honours `If-None-Match`; either use it for WORM promotion or confirm it stays covered by the advisory lock + registry + HEAD guard (it is not honoured by MinIO; ADR 0002).
- **[required before production]** `AwsKmsClient` implementing `edisc_core.kms.KmsClient` (GenerateDataKey/Decrypt/ReEncrypt with EncryptionContext), per-tenant CMKs with rotation enabled, key policies restricting use to the worker/API roles.
- Connection re-authorization flow: on `invalid_grant` or `DecryptionError`, mark the connection `error`, emit a custody event, notify the tenant (M13+).
- Scheduled KEK rotation job calling `rewrap_tenant_tokens` per tenant, with custody events.
- **[required before production]** Microsoft Graph app authentication with a certificate (not a client secret), stored in the secret manager, with a rotation runbook (ADR 0009).
- Slack connector: document and enforce "token rotation disabled" for the internal-app tier (validate_connection checks the token type and non-expiry).
- Periodic mutation-testing CI job (e.g. mutmut) on `packages/custody`, `packages/evidence` and `edisc_core.kms`/`envelope`/`canonical`, publishing a surviving-mutants report; this automates the manual "teeth" checks done per milestone.
- Rate-limit fairness across concurrent jobs within one tenant (e.g. per-job sub-buckets or weighted round-robin over a shared bucket) so one large job cannot starve another (ADR 0010).
- Test infra: run integration tests in parallel ephemeral stacks (pytest-xdist + per-worker compose project)
  if suite time grows; today one `edisc-test` stack per run.
- Throughput: make `guard_job_open` a statement-level check (or cache the job-open check per transaction) instead of
  a per-row `FOR SHARE` on the job row for every inserted item/link/event.
- Activity hang detection: `_ticking` heartbeats keep a deadlocked-but-alive activity alive until start-to-close
  (30 min). Add per-step watchdogs (DB statement timeout already bounds SQL; S3 calls have client timeouts).
- Re-authorization alerting delivery (email/webhook) for `alerts` rows; today alerts are records only (M13+).
- **[required before Phase 1 sign-off]** 1M-message resume soak on a cloud VM (>= 32 GB free disk after the
  corrected ~7 KB/message estimate, >= 16 GB RAM): `scripts/resume_soak.py --messages 1000000 --kills 10`. The laptop
  run was aborted at ~110k messages and later refused by the disk check (docs/runs/2026-10-01-*.md).
- Page-object compression (option 3), once real Slack exports are available to measure the ratio.
- Tenant-defined custom roles (ADR 0013 decision b: fixed roles for v1).
- Audit of metadata reads (job status, lists, reconciliation, custody results); content reads are audited (ADR 0013 d).
- Slack exports (ADR 0014): a sweeper that ends export uploads left `uploading` past
  `EDISC_EXPORT_UPLOAD_TTL_DAYS` (status `expired`, custody event, multipart upload aborted). Today a part
  sent after expiry gets 410 and the row stays `uploading`.
- Slack exports: MinIO aborts incomplete multipart uploads after its own `stale_uploads_expiry` (24 h) and
  ignores the lifecycle abort rule, so locally an upload must finish within a day; on AWS the `exports/`
  rule allows 7 days.
- Slack exports: `export_entries` costs ~150 bytes/row (3 GB at 20M entries). If very large exports become
  common, store only day files and unknown entries per row and keep counts for the rest.
