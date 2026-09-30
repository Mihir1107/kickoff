"""Evidence writer (ADR 0002, scheme C).

Pages  -> ``t/{tenant}/jobs/{job}/pages/{evidence_id}.json``: single pass straight into the WORM bucket.
          A page is a per-fetch artifact (exact bytes as received), so it gets its own key; idempotency
          comes from the items it contains, not from the object.
Files  -> staged, then ``t/{tenant}/files/sha256/{h[:2]}/{h}``: streamed into the unlocked staging bucket
          while hashing, then server-side copied into the content-addressed WORM key (skipped if it
          already exists: dedup within the tenant), verified at the destination, staging deleted.
          No client evidence ever touches worker disk.

Every WORM object is registered write-ahead in ``evidence_objects`` (pending -> complete|missing).
Retention is the rolling window from :mod:`edisc_evidence.retention`; a dedup hit only ever extends it.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client
from types_aiobotocore_s3.type_defs import CompletedPartTypeDef, CopySourceTypeDef

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_evidence.upload import Lock, UploadResult, rehash_object, stream_upload


class EvidenceIntegrityError(RuntimeError):
    """Stored bytes do not match the hash computed while streaming. Always an incident; never retried away."""


@dataclass(frozen=True)
class WrittenEvidence:
    evidence_id: uuid.UUID
    storage_key: str
    sha256: str
    size: int
    deduplicated: bool


def page_key(tenant_id: uuid.UUID, job_id: uuid.UUID, evidence_id: uuid.UUID) -> str:
    return f"t/{tenant_id}/jobs/{job_id}/pages/{evidence_id}.json"


def file_key(tenant_id: uuid.UUID, sha256: str) -> str:
    return f"t/{tenant_id}/files/sha256/{sha256[:2]}/{sha256}"


def _is_missing(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}


class EvidenceWriter:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], s3: S3Client, settings: Settings
    ) -> None:
        self._sessions = sessions
        self._s3 = s3
        self._settings = settings

    # ------------------------------------------------------------------ pages
    async def write_page(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        matter_retention_until: datetime,
        stream: AsyncIterable[bytes],
    ) -> WrittenEvidence:
        evidence_id = new_id()
        key = page_key(tenant_id, job_id, evidence_id)
        retain = effective_retain_until(self._settings, matter_retention_until)
        await self._register(tenant_id, job_id, evidence_id, key, "page", retain)

        async def record_upload(upload_id: str) -> None:
            async with tenant_tx(self._sessions, tenant_id) as s:
                await s.execute(
                    text(
                        "UPDATE evidence_objects SET upload_id = :u WHERE id = :id AND state = 'pending'"
                    ),
                    {"u": upload_id, "id": evidence_id},
                )

        try:
            result = await stream_upload(
                self._s3,
                bucket=self._settings.s3_evidence_bucket,
                key=key,
                stream=stream,
                part_size=self._settings.evidence_part_size_bytes,
                lock=Lock(retain),
                if_none_match=True,
                on_multipart_started=record_upload,
            )
        except BaseException:
            # The upload was aborted (or never completed): the object does not exist. Record that.
            await self._finalize(tenant_id, evidence_id, None)
            raise
        await self._finalize(tenant_id, evidence_id, result)
        return WrittenEvidence(evidence_id, key, result.sha256, result.size, deduplicated=False)

    # ------------------------------------------------------------------ files
    async def write_file(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        matter_retention_until: datetime,
        stream: AsyncIterable[bytes],
    ) -> WrittenEvidence:
        settings = self._settings
        staging_key = f"t/{tenant_id}/staging/{new_id()}"
        staged = await stream_upload(
            self._s3,
            bucket=settings.s3_staging_bucket,
            key=staging_key,
            stream=stream,
            part_size=settings.evidence_part_size_bytes,
            lock=None,
            if_none_match=True,
        )
        try:
            return await self._promote(
                tenant_id, job_id, matter_retention_until, staging_key, staged
            )
        finally:
            await self._s3.delete_object(Bucket=settings.s3_staging_bucket, Key=staging_key)

    async def _promote(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        matter_retention_until: datetime,
        staging_key: str,
        staged: UploadResult,
    ) -> WrittenEvidence:
        key = file_key(tenant_id, staged.sha256)
        retain = effective_retain_until(self._settings, matter_retention_until)
        candidate = new_id()
        # Serialize writers of the same content: MinIO ignores If-None-Match on CopyObject, so the DB
        # advisory lock (held on one pooled connection for the whole promotion) is what prevents a
        # second copy from creating a shadowing version.
        async with self._sessions() as lock_session:
            await lock_session.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"), {"k": key}
            )
            try:
                async with tenant_tx(self._sessions, tenant_id) as s:
                    await s.execute(
                        text(
                            "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until)"
                            " VALUES (:id, :t, :j, :k, 'file', :r) ON CONFLICT (storage_key) DO NOTHING"
                        ),
                        {"id": candidate, "t": tenant_id, "j": job_id, "k": key, "r": retain},
                    )
                    row = (
                        await s.execute(
                            text(
                                "SELECT id, state, sha256, retain_until FROM evidence_objects WHERE storage_key = :k"
                            ),
                            {"k": key},
                        )
                    ).one()
                if row.state == "complete":
                    if row.sha256 != staged.sha256:
                        raise EvidenceIntegrityError(
                            f"{key}: registry sha256 {row.sha256} != {staged.sha256}"
                        )
                    await self._extend_retention(tenant_id, row.id, key, row.retain_until, retain)
                    return WrittenEvidence(
                        row.id, key, staged.sha256, staged.size, deduplicated=True
                    )

                if row.state != "pending":
                    raise EvidenceIntegrityError(f"{key}: unexpected registry state {row.state}")
                # pending (ours, or left by a crashed writer): make the object exist, verified
                if not await self._destination_matches(key, staged):
                    await self._copy_into_worm(staging_key, key, staged, row.retain_until)
                    if not await self._destination_matches(key, staged):
                        raise EvidenceIntegrityError(
                            f"{key}: destination does not match the streamed hash"
                        )
                await self._finalize(tenant_id, row.id, staged)
                return WrittenEvidence(
                    row.id, key, staged.sha256, staged.size, deduplicated=row.id != candidate
                )
            finally:
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key}
                )
                await lock_session.commit()

    async def _copy_into_worm(
        self, staging_key: str, key: str, staged: UploadResult, retain_until: datetime
    ) -> None:
        settings, s3 = self._settings, self._s3
        source: CopySourceTypeDef = {"Bucket": settings.s3_staging_bucket, "Key": staging_key}
        lock_args: dict[str, Any] = {
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": ensure_utc(retain_until),
        }
        if staged.size <= settings.evidence_single_copy_max_bytes:
            # Server recomputes a full-object SHA-256 for the destination (verified afterwards).
            await s3.copy_object(
                Bucket=settings.s3_evidence_bucket,
                Key=key,
                CopySource=source,
                ChecksumAlgorithm="SHA256",
                IfNoneMatch="*",
                **lock_args,
            )
            return
        # > single-copy limit (5 GB on S3): UploadPartCopy. Lock set at creation; abort on any failure.
        created = await s3.create_multipart_upload(
            Bucket=settings.s3_evidence_bucket, Key=key, **lock_args
        )
        upload_id = created["UploadId"]
        try:
            parts: list[CompletedPartTypeDef] = []
            step = settings.evidence_copy_part_size_bytes
            for number, start in enumerate(range(0, staged.size, step), start=1):
                end = min(start + step, staged.size) - 1
                resp = await s3.upload_part_copy(
                    Bucket=settings.s3_evidence_bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=number,
                    CopySource=source,
                    CopySourceRange=f"bytes={start}-{end}",
                )
                parts.append({"PartNumber": number, "ETag": resp["CopyPartResult"]["ETag"]})
            await s3.complete_multipart_upload(
                Bucket=settings.s3_evidence_bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
                IfNoneMatch="*",
            )
        except BaseException as exc:
            try:
                await s3.abort_multipart_upload(
                    Bucket=settings.s3_evidence_bucket, Key=key, UploadId=upload_id
                )
            except Exception as abort_exc:  # noqa: BLE001 - attached to the original error
                exc.add_note(f"abort of multipart copy {upload_id} also failed: {abort_exc!r}")
            raise

    async def _destination_matches(self, key: str, staged: UploadResult) -> bool:
        """True if the WORM object exists with exactly the streamed bytes; False if it does not exist.

        Uses the server's full-object SHA-256 when the store reports one (single-part copies). Otherwise,
        e.g. multipart copies whose checksum is composite or absent, re-reads and re-hashes the object.
        """
        try:
            head = await self._s3.head_object(
                Bucket=self._settings.s3_evidence_bucket, Key=key, ChecksumMode="ENABLED"
            )
        except ClientError as exc:
            if _is_missing(exc):
                return False
            raise
        if head["ContentLength"] != staged.size:
            raise EvidenceIntegrityError(
                f"{key}: size {head['ContentLength']} != streamed {staged.size}"
            )
        checksum = head.get("ChecksumSHA256")
        if checksum and "-" not in checksum:
            if base64.b64decode(checksum).hex() != staged.sha256:
                raise EvidenceIntegrityError(
                    f"{key}: stored full-object SHA-256 differs from the streamed hash"
                )
            return True
        digest, size = await rehash_object(
            self._s3, bucket=self._settings.s3_evidence_bucket, key=key
        )
        if (digest, size) != (staged.sha256, staged.size):
            raise EvidenceIntegrityError(f"{key}: re-read hash differs from the streamed hash")
        return True

    async def _extend_retention(
        self,
        tenant_id: uuid.UUID,
        evidence_id: uuid.UUID,
        key: str,
        current: datetime,
        wanted: datetime,
    ) -> None:
        if wanted <= current:
            return
        await self._s3.put_object_retention(
            Bucket=self._settings.s3_evidence_bucket,
            Key=key,
            Retention={"Mode": "COMPLIANCE", "RetainUntilDate": ensure_utc(wanted)},
        )
        async with tenant_tx(self._sessions, tenant_id) as s:
            await s.execute(
                text(
                    "UPDATE evidence_objects SET retain_until = :r WHERE id = :id AND retain_until < :r"
                ),
                {"r": wanted, "id": evidence_id},
            )

    # ------------------------------------------------------------------ registry
    async def _register(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        evidence_id: uuid.UUID,
        key: str,
        kind: str,
        retain: datetime,
    ) -> None:
        async with tenant_tx(self._sessions, tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until)"
                    " VALUES (:id, :t, :j, :k, :kind, :r)"
                ),
                {
                    "id": evidence_id,
                    "t": tenant_id,
                    "j": job_id,
                    "k": key,
                    "kind": kind,
                    "r": retain,
                },
            )

    async def _finalize(
        self, tenant_id: uuid.UUID, evidence_id: uuid.UUID, result: UploadResult | None
    ) -> None:
        async with tenant_tx(self._sessions, tenant_id) as s:
            if result is None:
                await s.execute(
                    text(
                        "UPDATE evidence_objects SET state = 'missing' WHERE id = :id AND state = 'pending'"
                    ),
                    {"id": evidence_id},
                )
            else:
                await s.execute(
                    text(
                        "UPDATE evidence_objects SET state = 'complete', sha256 = :h, size_bytes = :n,"
                        " completed_at = now() WHERE id = :id AND state = 'pending'"
                    ),
                    {"h": result.sha256, "n": result.size, "id": evidence_id},
                )

    # ------------------------------------------------------------------ verification / recovery
    async def verify(self, *, tenant_id: uuid.UUID, evidence_id: uuid.UUID) -> bool:
        """Re-download and re-hash one object; True iff bytes match the registry. Streams, bounded memory."""
        async with tenant_tx(self._sessions, tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT storage_key, sha256, size_bytes, state FROM evidence_objects WHERE id = :id"
                    ),
                    {"id": evidence_id},
                )
            ).one()
        if row.state != "complete":
            return False
        digest, size = await rehash_object(
            self._s3, bucket=self._settings.s3_evidence_bucket, key=row.storage_key
        )
        return bool(digest == row.sha256 and size == row.size_bytes)

    async def recover_pending(self, *, tenant_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, int]:
        """Resolve every pending registry row of a job whose writers are gone (finalize / recovery).

        - object exists (the writer died after completing, before recording): re-hash it and mark it
          complete, so it is accounted for (an orphan unless items reference it);
        - object absent: abort its multipart upload if one was recorded, and mark the row missing.
          (File rows are never marked missing: their content-addressed key must stay writable. They
          stay pending and the next writer of that content completes them under the advisory lock.)
        """
        async with tenant_tx(self._sessions, tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id, storage_key, kind, upload_id FROM evidence_objects"
                        " WHERE job_id = :j AND state = 'pending'"
                    ),
                    {"j": job_id},
                )
            ).all()
        counts = {"completed": 0, "missing": 0, "left_pending": 0}
        bucket = self._settings.s3_evidence_bucket
        for row in rows:
            try:
                await self._s3.head_object(Bucket=bucket, Key=row.storage_key)
                exists = True
            except ClientError as exc:
                if not _is_missing(exc):
                    raise
                exists = False
            if exists:
                digest, size = await rehash_object(self._s3, bucket=bucket, key=row.storage_key)
                await self._finalize(tenant_id, row.id, UploadResult(digest, size, 0, None))
                counts["completed"] += 1
                continue
            if row.upload_id:
                try:
                    await self._s3.abort_multipart_upload(
                        Bucket=bucket, Key=row.storage_key, UploadId=row.upload_id
                    )
                except ClientError as exc:
                    if str(exc.response.get("Error", {}).get("Code")) != "NoSuchUpload":
                        raise
            if row.kind == "file":
                counts["left_pending"] += 1
                continue
            await self._finalize(tenant_id, row.id, None)
            counts["missing"] += 1
        return counts
