# Backlog

Ideas and later-phase work. Nothing here is in scope until promoted into a phase plan.

- S3 legal-hold flag in addition to per-matter retention (decided: backlog, 2026-09-30).
- Automate starting render workers of old renderer/Unicode/tzdata triples on an unroutable episode
  (a Job per triple, scale to zero when idle); v1 is a manual runbook (ADR 0017 §3).
- **[required before production]** Job custody package download endpoint (`edisc-verify --job-package`
  input for the UI): audited, permissioned like evidence content, streaming raw pages and files from
  their pinned versions. Deferred from M15 step 5 (2026-10-05); render packages are downloadable.
- Re-run WORM acceptance tests against real AWS S3 Object Lock before production.
- **[required before production]** Verify the vendored sRGB2014 ICC profile (SHA-256 `384b832d…c0a`, the copy WeasyPrint 70.0 bundles) byte for byte against the official file from color.org (scripted downloads got HTML in S1; download in a browser), and record the result in the profile's `SOURCE.md` (ADR 0018 §5.6).
- Report reproductions (ADR 0017 §4 applied to reports, ADR 0018 §6).
- Mirror the pinned `pgsty/minio` / `pgsty/mc` images to our own registry (upstream MinIO stopped publishing images; the fork is third-party).
- **[required before production]** Least-privilege S3 identity for the app instead of root credentials (evidence writer needs PutObject*, GetObject*, no Delete*).
- AWS KMS implementation of `KmsClient`.
- Stronger tenant binding than a settable GUC (per-tenant DB roles or signed tenant context), see ADR 0007 residual risk.
- RFC 3161 trusted timestamping of custody anchors (TSA token stored next to each anchor) for court-grade proof of time.
- **[required before production]** Bucket policy denying `s3:PutObject` without `If-None-Match` on anchor/evidence prefixes, and denying `s3:DeleteObject` (delete markers) for the app identity.
- Anchor the exported package manifest to WORM at export time so the manifest hash itself is attested.
- **[with Phase 2 bulk export ingestion]** Custody lock-narrowing reorder (deferred FK, `append_batch` last), then re-measure. Per-unit chains stay deferred (docs/runs/2026-09-30-custody-contention.md).
- **[required before production]** Post-close destruction workflow: after a matter (or client) is closed
  and every object's retention has expired, destroy the evidence deliberately (every version, verified
  gone), record each destruction in custody, and issue a certificate of destruction listing what was
  destroyed (evidence ids, hashes, keys, versions) and when. Must refuse anything still referenced by an
  open matter or client, or under legal hold.
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
- **[required before production]** Retention extension for tenant-stream audit anchors (job_id null, no
  matter or client owner): extend while the tenant is active, or re-anchor periodically (ADR 0002).
- Retention extension cost: the matter query walks every link of an active matter's jobs on each run;
  for very large matters, track a per-matter "next extension due" date instead.
- Per-tenant export upload quota (bytes in flight and stored) and a limit on concurrent export uploads
  per tenant (ADR 0014).
- Slack exports: a 5 GB streaming archive ingestion run with memory measured (ADR 0014 section 3), on the
  same cloud VM as the 1M soak (laptop disk too small to keep it next to the test stack). The reader's
  bounded memory is already tested on a 3M-entry synthetic directory and by fuzzing.
- RSMF renders that would need ZIP64 in a part's `rsmf.zip`: since §11 (renderer 1.3.0) attachments
  leave the zip as natives and the entry count splits parts, so only a manifest that is itself near
  4 GiB could still need it (it raises `ZipLimitError`). Not expected for Slack; revisit only if seen.
- A real multi-GB native (server-side copy of a 4 GiB+ file, the verification read, a package over
  4 GiB with natives embedded) measured on the cloud VM with the 1M soak (ADR 0015 §20.8).
- Cross-render native dedupe for oversized attachments (one native per matter and SHA-256, shared by
  renders instead of one copy per render): measure the storage and server-side copy cost on the cloud
  VM first (ADR 0015 §20.13).
- **[hard requirement of the Phase 3 live Slack connector]** Emit conversation metadata as versioned
  `conversation_snapshot` items through the directory unit: name, type, topic, purpose, members,
  archived state (channels are renamed; every state is kept). Normalizer, loader and renderer already
  handle them, and the dummy connector emits them (ADR 0004 amendment 2026-10-04; ADR 0015 §13).
- `edisc-verify` on JOB custody packages (`verify_package`) does not flag files the manifest does not
  account for; make it strict like render packages (with the same `--tolerate-os-metadata` rule).
