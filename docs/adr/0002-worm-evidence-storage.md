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
| File / attachment | `t/{tenant}/files/sha256/{h[:2]}/{h}` | Streamed into the **unlocked staging bucket** while hashing, then server-side copied into the content-addressed WORM key, verified, staging deleted | Same bytes give the same key: dedup within the tenant, never across tenants |
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

### Retention: rolling window, extended while the matter is active
- Retain-until on write = `min(matter.retention_until, now + EDISC_EVIDENCE_RETENTION_WINDOW_DAYS)`,
  default **90 days**, never the full matter retention upfront.
- A **matter-level extension job** pushes retention forward while the matter is active (backlog,
  **required before production**). When the matter closes, extension stops and objects expire on
  schedule. A client's destruction request at close can then be honoured once the window lapses;
  locking for years upfront would make it unfulfillable.
- A dedup hit from another matter only ever **extends** retention (PutObjectRetention in COMPLIANCE can
  extend, never shorten), in S3 and in the registry.
- Local/CI: `EDISC_EVIDENCE_RETENTION_OVERRIDE_DAYS` caps retention (1 day). It is rejected at startup
  outside local/ci.

### Failures and cleanup
- Any failure or cancellation during a multipart upload or copy **aborts the multipart upload**
  (shielded from cancellation). If the abort itself fails, that is attached to the original error,
  never swallowed.
- Hard kills (SIGKILL) leave an open upload and a `pending` row with its `upload_id`. The job's
  finalizer runs `recover_pending()`:
  - if the object exists, re-hash it and mark it complete;
  - otherwise abort the upload and mark the row missing.

  File rows stay pending (their content key must remain writable) and are completed by the next
  writer of that content.
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
