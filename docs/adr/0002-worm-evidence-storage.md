# ADR 0002: WORM evidence storage (S3 Object Lock, COMPLIANCE)

Status: Accepted (2026-09-30)

## Context
Raw collected data must be provably unmodified from collection to production, and application code
must be physically unable to alter or delete it.

## Decision
- S3-compatible storage with **Object Lock in COMPLIANCE mode**. Bucket created with object lock at
  creation time and a small bucket default retention as a safety net. Locally: MinIO.
- **What is stored:**
  - Each raw API **page** is stored as its exact received bytes, one object per page. The page object's
    SHA-256 is recorded (`evidence_objects.sha256`).
  - **Attachments/files are always separate objects**, never embedded in pages.
  - Each item records a pointer `(storage_key, json_path)` into its page (e.g. `$.messages[17]`) and its
    own `raw_hash` = SHA-256 of the RFC 8785 canonical JSON of that sub-document.
  - At job end a **custody seal** object (final chain head) is written (ADR 0003).
- **Per-object retention** = `matters.retention_until` (NOT NULL; jobs cannot start without it), set on
  `PutObject` / `CreateMultipartUpload`. Legal hold is backlog.
  - Local/CI only (`EDISC_ENV` ∈ local, ci): `EDISC_EVIDENCE_RETENTION_OVERRIDE_DAYS` caps retention to a
    short period, because COMPLIANCE retention can never be shortened. The setting is rejected at startup
    in any other environment.
- **No overwrite:** every write uses `If-None-Match: *`. With Object Lock the bucket is versioned, so a
  plain PUT would silently create a new version; the conditional write makes it fail instead.
- **Write-ahead registry:** an `evidence_objects` row (`pending`) is inserted before upload and completed
  after, so a crash can orphan an object but never leave an unaccounted one. Orphans are reported.
- **Streaming:** SHA-256 is computed while uploading multipart parts (per-part checksums sent), with
  bounded memory. `verify(key)` re-downloads, re-hashes and compares to the recorded hash.
- **Image:** upstream MinIO stopped publishing container images (2025). We use the `pgsty/minio`
  community rebuild pinned by tag + digest. Mirroring it and re-running the WORM acceptance tests against
  AWS S3 Object Lock are in the backlog.

- **Plain S3 API only.** The evidence package uses standard S3 calls (PutObject/multipart with
  `ObjectLockMode`/`ObjectLockRetainUntilDate`, `If-None-Match`, GetObject, HeadObject,
  GetObjectRetention). No MinIO admin APIs or `mc` in application code, so production switches to AWS S3
  Object Lock with configuration only.
- **WORM acceptance test** (a plain DELETE on a versioned bucket just adds a delete marker and proves
  nothing, so the test targets versions):
  1. `DeleteObject` with the specific `VersionId` of the evidence object → must fail (AccessDenied).
  2. `PutObjectRetention` shortening retain-until (and with `BypassGovernanceRetention`) → must fail.
  3. Overwrite via `PutObject` with `If-None-Match: *` → must fail (412).
  4. After all attempts: `GetObject` of that exact `VersionId` still returns bytes that re-hash to the
     recorded SHA-256, and retention mode/date are unchanged.

## Consequences
- + Deletion/overwrite is impossible even with root credentials until retention expires.
- + Page-level objects preserve exact provenance and cut object count ~200× vs per-message objects.
- − Local data cannot be deleted except by wiping the volume (`make nuke`, local/ci only).
- − Item integrity checks must extract the sub-document from the page (cheap, pages are small).
