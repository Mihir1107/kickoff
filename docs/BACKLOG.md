# Backlog

Ideas and later-phase work. Nothing here is in scope until promoted into a phase plan.

- S3 legal-hold flag in addition to per-matter retention (decided: backlog, 2026-09-30).
- Automate starting render workers of old renderer/Unicode/tzdata triples on an unroutable episode
  (a Job per triple, scale to zero when idle); v1 is a manual runbook (ADR 0017 §3).
- Re-run WORM acceptance tests against real AWS S3 Object Lock before production.
- Archive ended `sessions` and `login_attempts` rows after a retention period (no-delete rule:
  an owner-run archiving job, like the other insert-only tables; ADR 0016 step-2 note 9).
- Archive superseded directory generations (`directory_snapshots` status `superseded` and their
  `directory_entries`) after a retention period: kept by the no-delete rule, they grow with every
  refresh (one generation per connection per day by default; ADR 0016 "closing M17" note 3).
- **[required before production]** IdP-side deprovisioning reaches sessions without waiting for
  the 12 h absolute lifetime (ADR 0016 A2.6, M17 decision D8): SCIM deprovisioning (a principal
  deactivated by the IdP ends its sessions in the deactivation's transaction, like an admin
  deactivation) and OIDC back-channel logout (a verified `logout_token` revokes the sessions of its
  `sub`/`sid`).
- **[required before production]** Verify the vendored sRGB2014 ICC profile (SHA-256 `384b832d…c0a`, the copy WeasyPrint 70.0 bundles) byte for byte against the official file from color.org (scripted downloads got HTML in S1; download in a browser), and record the result in the profile's `SOURCE.md` (ADR 0018 §5.6).
- Report reproductions (ADR 0017 §4 applied to reports, ADR 0018 §6).
- **[required before production; needs cloud]** Report image delivery (ADR 0018 §22.8, §6): push
  the admitted `deploy/report-image` image (target `report`) to our own registry by digest, write
  `deploy/render-images.json` (per image digest: the identities it serves, e.g.
  `report: r<renderer>-p<toolchain12>-u<unicode>-g<glyphs12>`, git commit, build date, status,
  retention), set `EDISC_REPORT_TOOLCHAIN_ID` for report creators from it; and production deployment
  files for report workers (3 GiB memory limit, no swap, CPU 1, `EDISC_REPORT_PDF_CONCURRENCY=1`,
  scale out by replicas, one queue per runtime identity). The admission gate (PDF goldens in the
  image) already runs in CI.
- Mirror the pinned `pgsty/minio` / `pgsty/mc` images to our own registry (upstream MinIO stopped publishing images; the fork is third-party).
- **[required before production]** Least-privilege S3 identity for the app instead of root credentials (evidence writer needs PutObject*, GetObject*, no Delete*).
- AWS KMS implementation of `KmsClient`.
- Stronger tenant binding than a settable GUC (per-tenant DB roles or signed tenant context), see ADR 0007 residual risk.
- RFC 3161 trusted timestamping of custody anchors (TSA token stored next to each anchor) for court-grade proof of time.
- **[required before production]** Bucket policy denying `s3:PutObject` without `If-None-Match` on anchor/evidence prefixes, and denying `s3:DeleteObject` (delete markers) for the app identity.
- **[required before production]** A standalone `edisc-verify` package that installs WITHOUT the
  rest of the codebase, for third-party experts (docs/VERIFYING.md). Today the install needs the
  workspace (`uv sync --frozen --no-dev --package edisc-renderers`, which also pulls the database and
  S3 client libraries the verifier never uses). Needed:
  - its own distribution (`edisc-verify`): only the offline modules (`edisc_custody` package / cli /
    chain / merkle / archive / package_archives / rsmf_check / render_files / report_files /
    job_export readers, `edisc_renderers.report.model` + `recompute`, the `edisc_core` pieces they
    import: canonical, lone_surrogates, schemas, time) and their third-party dependencies, pinned;
  - an import-boundary test: the distribution's modules import nothing outside it (no SQLAlchemy,
    asyncpg, aiobotocore, temporalio, FastAPI), extending the existing purity tests;
  - a CI job that builds it, installs it into an EMPTY venv from the built wheel(s) alone (no
    workspace, no network after download), and verifies the corpus job, render and report packages,
    including the tamper cases and `--job-package` recomputation; same results as the in-tree verifier;
  - reproducible build per release (fixed `SOURCE_DATE_EPOCH`, pinned build backend), published
    with its SHA-256 and the commit it was built from; VERIFYING.md's install section points to it;
  - versioning: the verifier states which package formats it reads (`/1`-`/3` custody, `/1`-`/3`
    render, `/1` report); a format bump releases a new verifier.
- Anchor the exported package manifest to WORM at export time so the manifest hash itself is attested.
- **[with Phase 2 bulk export ingestion]** Custody lock-narrowing reorder (deferred FK, `append_batch` last), then re-measure. Per-unit chains stay deferred (docs/runs/2026-09-30-custody-contention.md).
- **[required before production]** `items` lookups and foreign-key checks for a tenant the
  planner's statistics do not know yet (docs/runs/2026-10-19-export-validation-stale-statistics.md,
  the sweep).
  - **Seen:** generic and custom plans on a tenant-leading index with the key as a filter, reading
    the tenant's items so far, per FK check (`job_items`, `item_derivations` → `items`) and per
    batch (`idempotency_key = ANY`, the version lookup). A new tenant's first large collection is
    quadratic until autoanalyze runs (2 % of the table).
  - **Options:**
    - an ANALYZE of `items` whenever a job's inserted items double (bounds the stale work to a
      constant factor; the cheapest);
    - `pg_hint_plan` for the two pipeline lookups (available on RDS);
    - list or hash partitioning of `items` by tenant, with an ANALYZE of the new partition;
    - for the FK checks only: reference `items (id)` and enforce the tenant match with a trigger.
  - **Test** like `tests/integration/db/test_stale_statistics.py` (a tenant missing from the
    statistics, trap armed, bounded after).
- **[required before production]** Post-close destruction workflow: after a matter (or client) is closed
  and every object's retention has expired, destroy the evidence deliberately (every version, verified
  gone), record each destruction in custody, and issue a certificate of destruction listing what was
  destroyed (evidence ids, hashes, keys, versions) and when. Must refuse anything still referenced by an
  open matter or client, or under legal hold.
- AWS optimization for files > 5 GB: verify UploadPartCopy destinations by comparing the SHA-256 composite against our own per-range composite instead of a full re-read (ADR 0002).
- **[required before production]** Staging bucket: encrypted at rest (SSE-KMS), least-privilege access (the app may write/read/delete only its own staging prefix; nothing else may read it), alerting on objects older than the 1-day expiry.
- **[required before production]** On AWS, verify whether CopyObject honours `If-None-Match`; either use it for WORM promotion or confirm it stays covered by the advisory lock + registry + HEAD guard (it is not honoured by MinIO; ADR 0002).
- **[required before production]** `AwsKmsClient` implementing `edisc_core.kms.KmsClient` (GenerateDataKey/Decrypt/ReEncrypt with EncryptionContext), per-tenant CMKs with rotation enabled, key policies restricting use to the worker/API roles; and `KmsSigner` (GetPublicKey, Sign with MessageType=DIGEST, `RSASSA_PKCS1_V1_5_SHA_256`) for the platform client-authentication key (an `RSA_3072` `SIGN_VERIFY` key per environment, only the API role may Sign; M17 step 3b).
- **[hardening, required before production]** Database connection budget and pooling (pools are fixed
  since ADR 0015 §24.13; a request beyond capacity waits `EDISC_PG_POOL_TIMEOUT_MS`, then 503
  `db_pool_exhausted` / a transient activity error).
  - **Budget:** every process holds its full pool from start-up: API replica 20, worker process 20
    (+ 2 sweeper), plus one short-lived DIRECT connection per concurrent evidence content lock
    (`EvidenceWriter._content_lock`, outside the pool; also a SCRAM handshake on the loop each time).
    Sum over API replicas and worker processes (+ content locks, + migrations and operator tools, +
    `superuser_reserved_connections`) must stay below Postgres `max_connections` (300 in compose;
    managed Postgres sizes it by instance class). A deployment check computes it from the replica
    counts and refuses a plan over budget; pool sizes become settings per process type.
  - **PgBouncer in transaction mode** in front of Postgres once the budget does not fit. What it
    needs from us: (1) asyncpg's prepared-statement cache off or made safe (`statement_cache_size=0`
    or PgBouncer >= 1.21 `max_prepared_statements`; SQLAlchemy's asyncpg dialect also names its
    prepared statements, `prepared_statement_name_func` with unique names); (2) the RLS tenant
    setting ONLY ever set with `SET LOCAL` semantics (a session-level `SET` would leak one tenant's
    context to another client's transaction on the same server connection). Verified 2026-10-15:
    every write of `app.tenant_id` is transaction-local (`tenant_tx`: `set_config(..., true)`; the
    `create_tenant` definer function: `set_config(..., true)` and restores it; the test helper in
    `tests/integration/db/conftest.py`: `true`); no session-level `SET` exists in application code.
    (3) The connection-level settings we pass at start-up (`search_path`, `lock_timeout`,
    `idle_in_transaction_session_timeout` as asyncpg `server_settings`) are not forwarded by
    PgBouncer in transaction mode: move them to `ALTER ROLE edisc_app SET ...` (or SET LOCAL per
    transaction). (4) The content lock's SESSION-level advisory lock cannot go through a transaction
    pooler: it keeps its direct connection to Postgres (or becomes a lease row). (5) A test that runs
    the suite's tenant-isolation and RLS tests through PgBouncer.
- **[required before production for Entra tenants with many groups]** Fetch a user's groups from
  Microsoft Graph when the ID token carries the overage marker (`_claim_names` / `_claim_sources`,
  `hasgroups`) instead of refusing the sign-in (`edisc_api.idp_groups`, today: `groups_overage`).
  Needs a Graph token for that user's Entra tenant (client credentials with our `private_key_jwt`
  signer and `GroupMember.Read.All`, or the delegated `getMemberObjects` with the sign-in's access
  token), transitive membership (`getMemberObjects` with `securityEnabledOnly`), a bound on the
  number of groups and on the call's time (a slow Graph must fail the sign-in loudly, never grant
  fewer roles), and the same for bearer tokens (or a cached membership with an expiry). Until then
  the advice is Entra's "Groups assigned to the application" filter.
- Job streams: batch every feed's `HEAD` poll into ONE statement per tick per process (today one
  per job per second: about 1,000 statements a second for 500 distinct jobs on one process,
  docs/runs/2026-10-15-sse-500-streams.md); measure several API processes and over a real network.
- Connection re-authorization flow: on `invalid_grant` or `DecryptionError`, mark the connection `error`, emit a custody event, notify the tenant (M13+).
- Scheduled KEK rotation job calling `rewrap_tenant_tokens` per tenant, with custody events.
- **[required before production]** Microsoft Graph app authentication with a certificate (not a client secret), stored in the secret manager, with a rotation runbook (ADR 0009).
- Slack connector: document and enforce "token rotation disabled" for the internal-app tier (validate_connection checks the token type and non-expiry).
- Periodic mutation-testing CI job (e.g. mutmut) on `packages/custody`, `packages/evidence` and `edisc_core.kms`/`envelope`/`canonical`, publishing a surviving-mutants report; this automates the manual "teeth" checks done per milestone.
- Rate-limit fairness across concurrent jobs within one tenant (e.g. per-job sub-buckets or weighted round-robin over a shared bucket) so one large job cannot starve another (ADR 0010).
- Test infra: run integration tests in parallel ephemeral stacks (pytest-xdist + per-worker compose project)
  if suite time grows; today one `edisc-test` stack per run (an exclusive lock, `scripts/stack_lock.py`,
  stops two runs sharing it; CI shards one stack per runner).
- Audit the other per-chunk-yield sites for the single-LARGE-chunk bound that `aenvelope` now has
  (ADR 0015 §24.6): `edisc_core.jsonstream` yields per chunk but scans a coalesced ~8 MiB window
  (~0.4 s, §24), and `edisc_custody.archive.open_entry` inflates per chunk; bound each to a fixed work
  size per loop turn like the RSMF envelope, or confirm their chunks are already small in every caller.
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
- Slack exports, hostile conversation ids (ADR 0018 §28.1, §30): collect the messages of a
  conversation whose id (or team, or the export's workspace) holds a lone surrogate under its
  deterministic stand-in key (`lone-surrogate:<hex code units>`, `substitute_key`) instead of
  leaving every day file a gap. Today none of its messages becomes an item (they stay in the locked
  export, each unit a gap with `conversation_id_lone_surrogate`). Needs: item identities and
  idempotency keys built from the stand-in (it is ASCII, lossless and never equal to a source id),
  the exact id kept beside it (`export_conversations.exact`), the RSMF conversation id written valid
  with `edisc.lone_surrogates`, a live-API counterpart decision (the same id from the Slack API must
  map to the same stand-in), and the corpus case `export_hostile_channel_id` turned from "every day
  file a gap" into "every message collected and rendered".
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
- Log redaction (`edisc_core.redaction.register_secret`) recompiles one regex over EVERY secret the
  process has registered, on each new secret, and `redact_text` runs it on every log line: both grow
  with the number of decrypted tokens a long-lived worker has seen (66-87 ms per registration on the
  event loop at the end of the integration suite, ADR 0015 §24). Bound it (per-connection scrubbers
  that are dropped with the connection, or a multi-pattern matcher) before a worker serves many
  tenants.
- The render loader's `_index` still fetches every `job_items` link of a conversation in one query
  (decoded on the event loop by asyncpg): bounded only by the conversation's size. Renders should
  walk the conversation day by day through `RenderLoader.day_slice` (built for the preview, ADR 0018
  §29) once the render's whole-job reconciliation is fed per day.
- Re-measure the preview's forced anchor per page view on the cloud VM with production S3 latencies
  (docs/runs/2026-10-12-preview-audit-burst.md; group commit is the fallback of ADR 0018 §10).
- **[hard requirement of the Phase 3 live Slack and Teams connectors]** Implement
  `Connector.check_credentials` (M17 step 8, ADR 0016 step-8 note 8): Slack `auth.test` with the
  stored bot token, Microsoft a client-credentials token for the bound `tid` with the platform
  signer (`edisc_api.client_auth.ClientSigner` must then be reachable from workers, or the signer
  moves to a shared package). Until then `check-connections` counts their connections `unchecked`.
