# ADR 0002: WORM evidence storage (S3 Object Lock, COMPLIANCE)

Status: Accepted (2026-09-30), implemented in M6 (`packages/evidence`, migration 0005)

## Context
Raw collected data must be provably unmodified from collection to production, and application code
must be physically unable to alter or delete it. At the same time, clients may require destruction of
evidence when a matter closes, and COMPLIANCE-mode retention can never be shortened.

## Decision

### Store and API
- S3-compatible storage with **Object Lock in COMPLIANCE mode**. The evidence bucket is created with
  Object Lock and a small default retention as a safety net. Locally: MinIO (`pgsty/minio` community
  rebuild, pinned by digest).
- **Plain S3 API only:** PutObject, multipart, CopyObject/UploadPartCopy, HeadObject, GetObject,
  Get/PutObjectRetention and ListObjectVersions. There are no MinIO admin calls in application code,
  so production switches to AWS S3 with configuration only.

### What is stored, and the key scheme ("C")
| Kind | Key | Write path | Idempotency / dedup |
|---|---|---|---|
| Raw API page (exact bytes) | `t/{tenant}/jobs/{job}/pages/{evidence_id}.json` | Streamed directly into the WORM bucket (single pass, hashing while uploading) | A page is a per-fetch artifact whose bytes differ between fetches (volatile fields), so every attempt gets its own key. Idempotency comes from the items (ADR 0004). |
| File / attachment | `t/{tenant}/files/sha256/{h[:2]}/{h}` | Streamed into the **unlocked staging bucket** while hashing, then server-side copied into the content-addressed WORM key, verified, staging deleted. **Small files: direct PUT, no staging (amendment below)** | Same bytes give the same key: dedup within the tenant, never across tenants |
| Custody anchor | `custody-anchors/{tenant}/{stream}/{seq}.json` | Single PUT (ADR 0003) | Deterministic body |

- Items point at `(storage_key, json_path)` and carry their own `raw_hash`. Files are never embedded in
  pages.
- **Evidence hash** = our own streaming SHA-256 of the full bytes. S3 multipart checksums are
  composite (`…-N`) and are used **only for transport integrity** (a SHA-256 on every part). They are
  never used as the evidence hash.
- **Lock from the first moment:** Object Lock mode and retain-until are set on PutObject,
  CreateMultipartUpload and CopyObject themselves, so a completed object is never unlocked, even
  briefly.
- **No overwrite:** PutObject and CompleteMultipartUpload use `If-None-Match: *`. MinIO **ignores**
  `If-None-Match` on CopyObject (verified), which would let a second copy create a shadowing version.
  So file promotion holds a Postgres advisory lock on the key, re-reads the registry and HEADs the
  destination before copying. At most one copy per key is ever made; tested with concurrent writers
  (exactly one version).
- **Version pinning:** the registry stores the S3 `VersionId` we wrote. **Every read goes by that
  VersionId, never "latest":** `EvidenceWriter.open` (review), `verify`, export, re-hash checks and
  anchors. A version written at the same key outside our control (a bug, another service, an operator)
  is never served. `verify` and export report it (`shadow_versions`), and `edisc-verify` fails the
  package with a "storage incident" while still verifying the pinned original.
- **Source-hash provenance:** `source_sha256` (origin `collection`) is the hash of the bytes as streamed
  **from the source**. It is persisted on the pending row **before the object can exist in WORM**:
  - pages: before PutObject / CompleteMultipartUpload;
  - files: before the staging→WORM copy;
  - anchors: at insert.

  A DB trigger lets a row complete only with `sha256 = source_sha256` and a pinned `version_id`. So a
  hash computed from storage can never stand in for the collection-time hash; staging is the
  unprotected window this guards.
- **Write-ahead registry** (`evidence_objects`): the row is inserted `pending` before upload, and
  records the multipart `upload_id` as soon as the upload starts. It becomes `complete` (hash, size)
  or `missing` (no object). Completed rows are final, except that `retain_until` may be extended.
  Nothing reaches WORM without a registry row.

### Files: staging bucket vs local temp spool (evaluated for M6)

| | Local encrypted temp spool | **Staging bucket + server-side copy (chosen)** |
|---|---|---|
| Client evidence on worker disk | Yes: needs an encrypted ephemeral volume, cleanup after SIGKILL (startup sweep) and a disk budget of cap × concurrency | **None** |
| Dedup for very large files | Lost above the spool cap (fallback to job-scoped keys) | **All sizes** |
| Reads from the source | 1 (then 2 local passes) | **1** |
| S3 requests | 1 write | 1 staging write + 1 server-side copy (+ 1 delete); transient staging storage |
| Unlocked copy of client data | Temp file on disk | Staging object for seconds to minutes (SSE; 1-day expiry lifecycle; app may delete there only) |
| Conditional write on promotion | `If-None-Match` on Complete (honoured) | CopyObject `If-None-Match` ignored by MinIO → DB advisory lock + HEAD |
| Tamper window | Local disk | Staging bucket; **detected**, because the destination is verified against the hash computed while streaming from the source, before staging existed |

**Chosen: staging bucket.** It removes local disk from the evidence path entirely (no volume
encryption, SIGKILL cleanup or disk budget) and keeps dedup for every size. The cost is one
server-side copy and short-lived staging objects.

**Destination verification** (before the registry row is marked complete):
- **≤ `EDISC_EVIDENCE_SINGLE_COPY_MAX_BYTES` (5 GB, the S3 CopyObject limit):** CopyObject with
  `ChecksumAlgorithm=SHA256`. The store recomputes a **full-object SHA-256** for the destination (verified
  on MinIO, for both single-part and multipart sources), and `HeadObject(ChecksumMode=ENABLED)` must
  equal our streamed hash.
- **> 5 GB:** UploadPartCopy. The destination checksum is composite, or absent: MinIO cannot build a
  SHA-256 composite from copied parts. The destination is therefore verified by **re-reading it in full
  and re-hashing** (streaming, bounded memory) before completion.
  - On AWS this can later be replaced by comparing the composite (`sha256(concat(part sha256s))-N`)
    with a composite we compute ourselves over the same byte ranges while streaming. That is an
    optimization, not a correctness requirement (backlog).
- A mismatch at any point raises `EvidenceIntegrityError`. It is never retried away.

### Amendment (2026-10-01): small-file direct path, dedup pre-check, parallel files
Measured (docs/runs/2026-10-01-storage-throughput-breakdown.md): file evidence was 58% of batch time,
~28 ms per file, and 73% of file writes were duplicates that still went through staging, the content
lock and a copy.

- **Small files** (size <= `EDISC_EVIDENCE_SMALL_FILE_MAX_BYTES`, default 8 MiB, never above the part
  size; 0 disables) are read from the source **into memory** (bounded), hashed, and **skip staging**:
  1. Under the same per-key advisory lock: the registry row is inserted (or found) with the
     **source hash persisted before any write** (unchanged rule).
  2. If no version exists: a single `PutObject` straight to the content-addressed WORM key with
     `If-None-Match: *` (honoured on PutObject), Object Lock set on the request, and
     `ChecksumSHA256` = our hash. The store rejects a body that does not match it.
  3. The written version is verified (`HeadObject` full-object SHA-256 = our hash) and pinned; the row
     completes with `sha256 = source_sha256` (DB trigger unchanged).
  - No unlocked copy of client data exists at any point on this path: the bytes go from worker memory
    straight into a locked object.
  - A file whose stream exceeds the threshold switches to the staging path without re-reading the
    source: the bytes already buffered are streamed first, then the rest of the same source stream.
- **Dedup pre-check** (both paths): once the hash is known, a registry row for the key in state
  `complete` is a dedup hit. The registry hash must match, retention is extended if needed (extend-only,
  idempotent), and nothing is uploaded or copied. No content lock is needed, because complete rows are
  final.
  - For small files the hash is known before any upload, so a duplicate costs one source read and one
    registry query.
- **Large files** keep the staging path exactly as above.
- **Parallel files per page:** a page's files are written concurrently with bounded concurrency
  (`EDISC_EVIDENCE_FILE_CONCURRENCY`, default 4). Worker memory is bounded by concurrency x small-file
  threshold (32 MiB by default).
  - Every source download still goes through the rate limiter.
  - The first unexpected failure cancels the page's other file writes and is raised unchanged (its error
    class decides retry); failures of the cancelled writes are attached to it as notes.
  - File refusals (`FileUnavailableError`) are per file and recorded, as before.
- Recovery is unchanged:
  - A small-file row that crashed after its PutObject has a persisted source hash and a matching
    version, so it is completed by `recover_pending`.
  - One that crashed before the write has no object and stays pending, writable for the next writer.

### Retention: rolling window, extended while the matter is active
- Retain-until on write = `min(matter.retention_until, now + EDISC_EVIDENCE_RETENTION_WINDOW_DAYS)`,
  default **90 days**, never the full matter retention upfront.
- A **matter-level extension job** pushes retention forward while the matter is active (backlog,
  **required before production**). When the matter closes, extension stops and objects expire on
  schedule. A client's destruction request at close can then be honoured once the window lapses;
  locking for years upfront would make it unfulfillable.
- **Extension floor (amended 2026-10-01):** a dedup hit extends an object only when its remaining
  retention has dropped below `EDISC_EVIDENCE_RETENTION_EXTEND_FLOOR_DAYS` (default 60 of the 90-day
  window), and then to the rolling target. Invariant: retention never drops below
  `min(now + floor, target)`. Above the floor, duplicates issue no `PutObjectRetention` (tested). Before
  this, every hit extended by the seconds the rolling target had moved.
- A dedup hit from another matter only ever **extends** retention (PutObjectRetention in COMPLIANCE can
  extend, never shorten), in S3 and in the registry.
- Local/CI: `EDISC_EVIDENCE_RETENTION_OVERRIDE_DAYS` caps retention (1 day). It is rejected at startup
  outside local/test/ci.
- Ephemeral test stack (amended after a disk-full incident: locked test evidence accumulated on the dev
  volume and could not be deleted): integration tests run on a separate compose project (`edisc-test`)
  whose volumes are destroyed after every run. Only there (`EDISC_ENV=test|ci`) is
  `EDISC_EVIDENCE_RETENTION_OVERRIDE_SECONDS` accepted (validated at startup and re-checked when computing
  retain-until); the bucket default retention is disabled (`EDISC_S3_DEFAULT_RETENTION_DAYS=0`) so every
  object carries only its explicit seconds-level lock. Local/test/ci evidence buckets also get lifecycle
  expiration (current 2 days, noncurrent 1 day, expired delete markers) as a second line of defence;
  staging/production buckets never get an expiry rule. `make up` and the test targets refuse to run below
  `MIN_FREE_GB` (default 15) of free disk.

### Failures and cleanup
- Any failure or cancellation during a multipart upload or copy **aborts the multipart upload**
  (shielded from cancellation). If the abort itself fails, that is attached to the original error,
  never swallowed.
- Hard kills (SIGKILL) leave an open upload and a `pending` row with its `upload_id`. The job's
  finalizer runs `edisc_custody.recovery.recover_job_evidence()`, which records an
  `evidence_recovered` custody event:
  - A stored version matches the **persisted source hash**: complete, pinned to that version.
  - Versions exist but none matches: integrity incident, raised.
  - No object: abort the upload. Page rows become missing; file rows stay pending (their content key
    must stay writable).
  - An object exists but **no source hash was persisted** (legacy/unknown): it is **never completed
    from storage bytes**. It is listed as `needs_refetch`. The caller re-reads the source, and
    `complete_by_refetch` requires a stored version with exactly those bytes. It records origin
    `refetch` and a custody event with `recovery_path = refetch_from_source`.
- Bucket-level backstop:
  - AWS: lifecycle `AbortIncompleteMultipartUpload` after 1 day (`infra/aws/*.json`), and expiration
    after 1 day on staging.
  - **MinIO rejects the `AbortIncompleteMultipartUpload` rule** (verified). It expires stale uploads
    itself (`api stale_uploads_expiry`, default 24h), and staging uses an ILM expiration rule.
  - MinIO also cannot list multipart uploads by prefix, which is why upload ids are recorded in the
    registry.

### WORM acceptance test (implemented: `tests/integration/evidence/test_crash_and_worm.py`)
A plain DELETE on a versioned bucket only adds a delete marker and proves nothing, so the test targets
versions:
1. `DeleteObject` with the specific `VersionId` fails (AWS: AccessDenied; MinIO: InvalidRequest
   "Object is WORM protected"), also with `BypassGovernanceRetention`.
2. `PutObjectRetention` shortening retain-until fails (with and without bypass), and so does
   downgrading the mode to GOVERNANCE.
3. `PutObject` with `If-None-Match: *` fails (412).
4. Afterwards, `GetObject` of that exact `VersionId` still re-hashes to the recorded SHA-256, and
   retention mode and date are unchanged.

## Consequences
- + Deletion or overwrite is impossible, even with root credentials, until retention lapses.
  Destruction at matter close stays achievable.
- + No client evidence on worker disk. Dedup at every size. Peak memory is about 3 parts, independent of
  object size (tested at 20 and 200 MiB).
- − A second write per file (the server-side copy), and transient unlocked staging objects.
- − Files over 5 GB cost a full destination re-read on MinIO.
- − The extension job is a production prerequisite: without it, objects of long matters would expire
  after the window.
