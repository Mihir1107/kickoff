"""Evidence writer and reader (ADR 0002, scheme C).

Pages  -> ``t/{tenant}/jobs/{job}/pages/{evidence_id}.json``: single pass straight into the WORM bucket.
Files  -> staged, then ``t/{tenant}/files/sha256/{h[:2]}/{h}``: streamed into the unlocked staging bucket
          while hashing, then server-side copied into the content-addressed WORM key (skipped if it
          already exists: dedup within the tenant), verified, staging deleted. No evidence on worker disk.

Provenance: the SHA-256 of the bytes as streamed from the source is persisted on the pending registry row
BEFORE the object can exist in WORM (before PutObject/CompleteMultipartUpload for pages, before the copy
for files). A row completes only with ``sha256 = source_sha256`` (DB trigger), so a storage-derived hash
never stands in for the collection-time hash.

Version pinning: the registry records the S3 VersionId we wrote. Every read (verify, export, review)
goes by that VersionId, never "latest", so a shadowing version written outside our control is never
served. Shadows are reported as storage incidents.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
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
from edisc_evidence.worm import list_versions


class EvidenceIntegrityError(RuntimeError):
    """Stored bytes do not match the collection-time hash. Always an incident; never retried away."""


@dataclass(frozen=True)
class WrittenEvidence:
    evidence_id: uuid.UUID
    storage_key: str
    sha256: str
    size: int
    version_id: str
    deduplicated: bool


@dataclass(frozen=True)
class VerifyResult:
    evidence_id: uuid.UUID
    bytes_match: bool  # the PINNED version re-hashes to the recorded hash
    shadow_versions: tuple[
        str, ...
    ]  # other versions (or delete markers) at the key: storage incidents

    @property
    def clean(self) -> bool:
        return self.bytes_match and not self.shadow_versions


@dataclass
class RecoveryReport:
    completed: list[uuid.UUID] = field(default_factory=list)  # matched the persisted source hash
    missing: list[uuid.UUID] = field(default_factory=list)  # no object; upload aborted
    left_pending: list[uuid.UUID] = field(
        default_factory=list
    )  # file rows: content key stays writable
    needs_refetch: list[uuid.UUID] = field(
        default_factory=list
    )  # object exists, no source hash persisted
    shadows: dict[uuid.UUID, list[str]] = field(default_factory=dict)


def page_key(tenant_id: uuid.UUID, job_id: uuid.UUID, evidence_id: uuid.UUID) -> str:
    return f"t/{tenant_id}/jobs/{job_id}/pages/{evidence_id}.json"


def file_key(tenant_id: uuid.UUID, sha256: str) -> str:
    return f"t/{tenant_id}/files/sha256/{sha256[:2]}/{sha256}"


class EvidenceWriter:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], s3: S3Client, settings: Settings
    ) -> None:
        self._sessions = sessions
        self._s3 = s3
        self._settings = settings
        self._bucket = settings.s3_evidence_bucket

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

        async def persist_source_hash(sha256: str, _size: int) -> None:
            await self._persist_source_hash(tenant_id, evidence_id, sha256, "collection")

        try:
            result = await stream_upload(
                self._s3,
                bucket=self._bucket,
                key=key,
                stream=stream,
                part_size=self._settings.evidence_part_size_bytes,
                lock=Lock(retain),
                if_none_match=True,
                on_multipart_started=record_upload,
                before_commit=persist_source_hash,
            )
        except BaseException:
            # Aborted or never completed: the object does not exist. Record that.
            await self._mark_missing(tenant_id, evidence_id)
            raise
        if not result.version_id:
            raise EvidenceIntegrityError(f"{key}: store returned no VersionId (versioning off?)")
        await self._complete(tenant_id, evidence_id, result.sha256, result.size, result.version_id)
        return WrittenEvidence(
            evidence_id, key, result.sha256, result.size, result.version_id, deduplicated=False
        )

    # ------------------------------------------------------------------ files
    async def write_file(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        matter_retention_until: datetime,
        stream: AsyncIterable[bytes],
    ) -> WrittenEvidence:
        staging_key = f"t/{tenant_id}/staging/{new_id()}"
        staged = await stream_upload(
            self._s3,
            bucket=self._settings.s3_staging_bucket,
            key=staging_key,
            stream=stream,
            part_size=self._settings.evidence_part_size_bytes,
            lock=None,
            if_none_match=True,
        )
        try:
            return await self._promote(
                tenant_id, job_id, matter_retention_until, staging_key, staged
            )
        finally:
            await self._s3.delete_object(Bucket=self._settings.s3_staging_bucket, Key=staging_key)

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
        # advisory lock (held on one pooled connection for the whole promotion) prevents a second copy.
        async with self._sessions() as lock_session:
            await lock_session.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"), {"k": key}
            )
            try:
                async with tenant_tx(self._sessions, tenant_id) as s:
                    # the source hash is persisted with the row, BEFORE any copy into WORM
                    await s.execute(
                        text(
                            "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind,"
                            " retain_until, source_sha256, source_hash_origin)"
                            " VALUES (:id, :t, :j, :k, 'file', :r, :h, 'collection')"
                            " ON CONFLICT (storage_key) DO NOTHING"
                        ),
                        {
                            "id": candidate,
                            "t": tenant_id,
                            "j": job_id,
                            "k": key,
                            "r": retain,
                            "h": staged.sha256,
                        },
                    )
                    row = (
                        await s.execute(
                            text(
                                "SELECT id, state, sha256, source_sha256, retain_until, version_id"
                                " FROM evidence_objects WHERE storage_key = :k"
                            ),
                            {"k": key},
                        )
                    ).one()
                if row.state == "complete":
                    if row.sha256 != staged.sha256:
                        raise EvidenceIntegrityError(f"{key}: registry sha256 != {staged.sha256}")
                    await self._extend_retention(
                        tenant_id, row.id, key, row.version_id, row.retain_until, retain
                    )
                    return WrittenEvidence(
                        row.id, key, staged.sha256, staged.size, row.version_id, deduplicated=True
                    )
                if row.state != "pending":
                    raise EvidenceIntegrityError(f"{key}: unexpected registry state {row.state}")
                if row.source_sha256 is None:  # left by an older writer: persist before the copy
                    await self._persist_source_hash(tenant_id, row.id, staged.sha256, "collection")
                elif row.source_sha256 != staged.sha256:
                    raise EvidenceIntegrityError(
                        f"{key}: persisted source hash differs from stream"
                    )

                version = await self._find_version(key, staged.sha256, staged.size)
                if version is None:
                    version = await self._copy_into_worm(staging_key, key, staged, row.retain_until)
                    if not await self._version_matches(key, version, staged.sha256, staged.size):
                        raise EvidenceIntegrityError(
                            f"{key}: copied version {version} != source hash"
                        )
                await self._complete(tenant_id, row.id, staged.sha256, staged.size, version)
                return WrittenEvidence(
                    row.id,
                    key,
                    staged.sha256,
                    staged.size,
                    version,
                    deduplicated=row.id != candidate,
                )
            finally:
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key}
                )
                await lock_session.commit()

    async def _copy_into_worm(
        self, staging_key: str, key: str, staged: UploadResult, retain_until: datetime
    ) -> str:
        """Server-side copy with Object Lock set at creation. Returns the new VersionId."""
        settings, s3 = self._settings, self._s3
        source: CopySourceTypeDef = {"Bucket": settings.s3_staging_bucket, "Key": staging_key}
        lock_args: dict[str, Any] = {
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": ensure_utc(retain_until),
        }
        if staged.size <= settings.evidence_single_copy_max_bytes:
            resp = await s3.copy_object(
                Bucket=self._bucket,
                Key=key,
                CopySource=source,
                ChecksumAlgorithm="SHA256",
                IfNoneMatch="*",
                **lock_args,
            )
            version = resp.get("VersionId")
        else:  # > single-copy limit (5 GB on S3): UploadPartCopy; abort on any failure
            created = await s3.create_multipart_upload(Bucket=self._bucket, Key=key, **lock_args)
            upload_id = created["UploadId"]
            try:
                parts: list[CompletedPartTypeDef] = []
                step = settings.evidence_copy_part_size_bytes
                for number, start in enumerate(range(0, staged.size, step), start=1):
                    end = min(start + step, staged.size) - 1
                    part = await s3.upload_part_copy(
                        Bucket=self._bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=number,
                        CopySource=source,
                        CopySourceRange=f"bytes={start}-{end}",
                    )
                    parts.append({"PartNumber": number, "ETag": part["CopyPartResult"]["ETag"]})
                done = await s3.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    IfNoneMatch="*",
                )
                version = done.get("VersionId")
            except BaseException as exc:
                try:
                    await s3.abort_multipart_upload(
                        Bucket=self._bucket, Key=key, UploadId=upload_id
                    )
                except Exception as abort_exc:  # noqa: BLE001 - attached to the original error
                    exc.add_note(f"abort of multipart copy {upload_id} also failed: {abort_exc!r}")
                raise
        if not version:
            raise EvidenceIntegrityError(f"{key}: store returned no VersionId for the copy")
        return version

    # ------------------------------------------------------------------ version-pinned inspection
    async def _versions(self, key: str) -> list[tuple[str, bool]]:
        return [
            (v.version_id, v.is_delete_marker)
            async for v in list_versions(self._s3, bucket=self._bucket, prefix=key)
            if v.key == key
        ]

    async def _version_matches(self, key: str, version_id: str, sha256: str, size: int) -> bool:
        """Does exactly this version hold ``sha256``? Uses the store's full-object SHA-256 when it reports
        one; otherwise (multipart copies: composite or absent checksum) re-reads and re-hashes it."""
        head = await self._s3.head_object(
            Bucket=self._bucket, Key=key, VersionId=version_id, ChecksumMode="ENABLED"
        )
        if head["ContentLength"] != size:
            return False
        checksum = head.get("ChecksumSHA256")
        if checksum and "-" not in checksum:
            return base64.b64decode(checksum).hex() == sha256
        digest, got = await rehash_object(
            self._s3, bucket=self._bucket, key=key, version_id=version_id
        )
        return (digest, got) == (sha256, size)

    async def _find_version(self, key: str, sha256: str, size: int) -> str | None:
        """The oldest version at ``key`` holding ``sha256``. Raises if versions exist but none match."""
        versions = [v for v, marker in await self._versions(key) if not marker]
        for version_id in versions:
            if await self._version_matches(key, version_id, sha256, size):
                return version_id
        if versions:
            raise EvidenceIntegrityError(
                f"{key}: {len(versions)} version(s) exist but none matches the source hash"
            )
        return None

    # ------------------------------------------------------------------ reads (always by pinned version)
    async def _pinned(self, tenant_id: uuid.UUID, evidence_id: uuid.UUID) -> Any:
        async with tenant_tx(self._sessions, tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT storage_key, sha256, size_bytes, state, version_id"
                        " FROM evidence_objects WHERE id = :id"
                    ),
                    {"id": evidence_id},
                )
            ).one()
        if row.state != "complete" or not row.version_id:
            raise EvidenceIntegrityError(
                f"evidence {evidence_id} is not complete with a pinned version"
            )
        return row

    async def open(
        self, *, tenant_id: uuid.UUID, evidence_id: uuid.UUID, chunk: int = 1 << 20
    ) -> AsyncIterator[bytes]:
        """Stream the evidence bytes for review/export: always the pinned version."""
        row = await self._pinned(tenant_id, evidence_id)
        resp = await self._s3.get_object(
            Bucket=self._bucket, Key=row.storage_key, VersionId=row.version_id
        )
        async with resp["Body"] as body:
            async for data in body.iter_chunks(chunk):
                yield data

    async def verify(self, *, tenant_id: uuid.UUID, evidence_id: uuid.UUID) -> VerifyResult:
        """Re-hash the pinned version against the registry, and report any other version at the key."""
        row = await self._pinned(tenant_id, evidence_id)
        digest, size = await rehash_object(
            self._s3, bucket=self._bucket, key=row.storage_key, version_id=row.version_id
        )
        shadows = tuple(v for v, _ in await self._versions(row.storage_key) if v != row.version_id)
        return VerifyResult(evidence_id, (digest, size) == (row.sha256, row.size_bytes), shadows)

    # ------------------------------------------------------------------ retention
    async def _extend_retention(
        self,
        tenant_id: uuid.UUID,
        evidence_id: uuid.UUID,
        key: str,
        version_id: str,
        current: datetime,
        wanted: datetime,
    ) -> None:
        if wanted <= current:
            return
        await self._s3.put_object_retention(
            Bucket=self._bucket,
            Key=key,
            VersionId=version_id,
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

    async def _persist_source_hash(
        self, tenant_id: uuid.UUID, evidence_id: uuid.UUID, sha256: str, origin: str
    ) -> None:
        async with tenant_tx(self._sessions, tenant_id) as s:
            result = await s.execute(
                text(
                    "UPDATE evidence_objects SET source_sha256 = :h, source_hash_origin = :o"
                    " WHERE id = :id AND state = 'pending' AND source_sha256 IS NULL"
                ),
                {"h": sha256, "o": origin, "id": evidence_id},
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise EvidenceIntegrityError(
                    f"evidence {evidence_id}: could not persist source hash"
                )

    async def _complete(
        self, tenant_id: uuid.UUID, evidence_id: uuid.UUID, sha256: str, size: int, version_id: str
    ) -> None:
        async with tenant_tx(self._sessions, tenant_id) as s:
            await s.execute(
                text(
                    "UPDATE evidence_objects SET state = 'complete', sha256 = :h, size_bytes = :n,"
                    " version_id = :v, completed_at = now() WHERE id = :id AND state = 'pending'"
                ),
                {"h": sha256, "n": size, "v": version_id, "id": evidence_id},
            )

    async def _mark_missing(self, tenant_id: uuid.UUID, evidence_id: uuid.UUID) -> None:
        async with tenant_tx(self._sessions, tenant_id) as s:
            await s.execute(
                text(
                    "UPDATE evidence_objects SET state = 'missing' WHERE id = :id AND state = 'pending'"
                ),
                {"id": evidence_id},
            )

    # ------------------------------------------------------------------ recovery
    async def recover_pending(self, *, tenant_id: uuid.UUID, job_id: uuid.UUID) -> RecoveryReport:
        """Resolve every pending registry row of a job whose writers are gone (finalize / recovery).

        - source hash persisted, a stored version matches it: complete, pinned to that version;
        - source hash persisted, versions exist but none match: integrity incident (raised);
        - no object: abort the recorded multipart upload; page rows -> missing; file rows stay pending
          (their content-addressed key must remain writable for the next writer of that content);
        - object exists but NO source hash was persisted: never completed from storage bytes. Listed in
          ``needs_refetch``: the caller re-reads the source and calls :meth:`complete_by_refetch`.

        Custody events for these outcomes are recorded by ``edisc_custody.recovery``.
        """
        async with tenant_tx(self._sessions, tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id, storage_key, kind, upload_id, source_sha256 FROM evidence_objects"
                        " WHERE job_id = :j AND state = 'pending' ORDER BY created_at"
                    ),
                    {"j": job_id},
                )
            ).all()
        report = RecoveryReport()
        for row in rows:
            versions = [v for v, marker in await self._versions(row.storage_key) if not marker]
            if versions and row.source_sha256 is None:
                report.needs_refetch.append(row.id)
                continue
            if versions:
                pinned: tuple[str, int] | None = None
                for version_id in versions:
                    head = await self._s3.head_object(
                        Bucket=self._bucket, Key=row.storage_key, VersionId=version_id
                    )
                    size = int(head["ContentLength"])
                    if await self._version_matches(
                        row.storage_key, version_id, row.source_sha256, size
                    ):
                        pinned = (version_id, size)
                        break
                if pinned is None:
                    raise EvidenceIntegrityError(
                        f"{row.storage_key}: no stored version matches the persisted source hash"
                    )
                await self._complete(tenant_id, row.id, row.source_sha256, pinned[1], pinned[0])
                report.completed.append(row.id)
                others = [v for v in versions if v != pinned[0]]
                if others:
                    report.shadows[row.id] = others
                continue
            if row.upload_id:
                try:
                    await self._s3.abort_multipart_upload(
                        Bucket=self._bucket, Key=row.storage_key, UploadId=row.upload_id
                    )
                except ClientError as exc:
                    if str(exc.response.get("Error", {}).get("Code")) != "NoSuchUpload":
                        raise
            if row.kind == "file":
                report.left_pending.append(row.id)
            else:
                await self._mark_missing(tenant_id, row.id)
                report.missing.append(row.id)
        return report

    async def complete_by_refetch(
        self, *, tenant_id: uuid.UUID, evidence_id: uuid.UUID, source: AsyncIterable[bytes]
    ) -> WrittenEvidence:
        """Recovery for a stored object with no persisted source hash: hash a fresh read FROM THE SOURCE
        (nothing is uploaded), require a stored version with exactly those bytes, persist that hash with
        origin 'refetch', and complete. Raises if the source and storage disagree."""
        h, size = hashlib.sha256(), 0
        async for chunk in source:
            h.update(chunk)
            size += len(chunk)
        digest = h.hexdigest()
        async with tenant_tx(self._sessions, tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT storage_key, state, source_sha256 FROM evidence_objects WHERE id = :id"
                    ),
                    {"id": evidence_id},
                )
            ).one()
        if row.state != "pending" or row.source_sha256 is not None:
            raise EvidenceIntegrityError(f"evidence {evidence_id} is not awaiting a refetch")
        version = await self._find_version(row.storage_key, digest, size)
        if version is None:
            raise EvidenceIntegrityError(
                f"{row.storage_key}: no stored object to compare the refetch to"
            )
        await self._persist_source_hash(tenant_id, evidence_id, digest, "refetch")
        await self._complete(tenant_id, evidence_id, digest, size, version)
        return WrittenEvidence(
            evidence_id, row.storage_key, digest, size, version, deduplicated=False
        )
