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
- Throughput: write a page's files concurrently (bounded, e.g. 4) with errors classified per file; today files
  are written one after another and dominate batch time for attachment-heavy pages (M12 measurements).
- Throughput: make `guard_job_open` a statement-level check (or cache the job-open check per transaction) instead of
  a per-row `FOR SHARE` on the job row for every inserted item/link/event.
- Activity hang detection: `_ticking` heartbeats keep a deadlocked-but-alive activity alive until start-to-close
  (30 min). Add per-step watchdogs (DB statement timeout already bounds SQL; S3 calls have client timeouts).
- Re-authorization alerting delivery (email/webhook) for `alerts` rows; today alerts are records only (M13+).
- **[required before Phase 1 sign-off]** 1M-message resume soak on a host with >= 64 GB free disk and >= 32 GB
  RAM (docs/runs/2026-10-01-resume-soak.md); the laptop run was aborted at ~110k messages to protect the disk.
