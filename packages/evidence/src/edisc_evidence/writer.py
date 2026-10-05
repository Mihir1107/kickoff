"""Evidence writer and reader (ADR 0002, scheme C).

Pages  -> ``t/{tenant}/jobs/{job}/pages/{evidence_id}.json``: single pass straight into the WORM bucket.
Files  -> ``t/{tenant}/files/sha256/{h[:2]}/{h}`` (dedup within the tenant):
          - small (<= ``evidence_small_file_max_bytes``): read into memory, hashed, then a single locked
            ``PutObject`` straight to the content key (no staging; ADR 0002 amendment);
          - large: streamed into the unlocked staging bucket while hashing, then server-side copied into
            the content key, verified, staging deleted. No evidence on worker disk either way.
          A key already ``complete`` in the registry is a dedup hit: nothing is uploaded or copied.

Provenance: the SHA-256 of the bytes as streamed from the source is persisted on the pending registry row
BEFORE the object can exist in WORM (before PutObject/CompleteMultipartUpload for pages, before the copy
for files). A row completes only with ``sha256 = source_sha256`` (DB trigger), so a storage-derived hash
never stands in for the collection-time hash.

Version pinning: the registry records the S3 VersionId we wrote. Every read (verify, export, review)
goes by that VersionId, never "latest", so a shadowing version written outside our control is never
served. Shadows are reported as storage incidents.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client
from types_aiobotocore_s3.type_defs import CompletedPartTypeDef, CopySourceTypeDef

from edisc_core.ids import new_id
from edisc_core.logs import get_logger
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until, extension_needed
from edisc_evidence.upload import Lock, UploadResult, rehash_object, stream_upload
from edisc_evidence.worm import list_versions

log = get_logger(__name__)


class EvidenceCopyTimeoutError(TimeoutError):
    """Promotion (copy into WORM) exceeded its timeout. Retryable: the row stays pending with its source
    hash, and the next writer of the same content completes it under the lock."""


class ContentLockTimeoutError(TimeoutError):
    """Waited longer than the copy timeout for another writer's content lock. Retryable."""


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


def production_key(tenant_id: uuid.UUID, render_id: uuid.UUID, name: str) -> str:
    return f"t/{tenant_id}/productions/{render_id}/{name}"


def native_key(tenant_id: uuid.UUID, render_id: uuid.UUID, sha256: str) -> str:
    """A render's native (ADR 0015 §11, §20.3): one object per (render, SHA-256). Production names
    never contain '/', so this never collides with an output file."""
    return f"t/{tenant_id}/productions/{render_id}/natives/sha256/{sha256}"


async def _no_hook(point: str) -> None:
    return None


async def _hash_stream(stream: AsyncIterable[bytes]) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    async for chunk in stream:
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


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

    # ------------------------------------------------------------------ productions (ADR 0015 §7)
    async def write_production(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        render_id: uuid.UUID,
        name: str,
        matter_retention_until: datetime,
        stream: Callable[[], AsyncIterable[bytes]],
    ) -> WrittenEvidence:
        """A render output (an `.rsmf` file) as locked evidence of kind ``production``. It is tied to the
        RENDERED job, so the job's matter owns its retention. The output is reproducible, so ``stream``
        is a factory, and a retry re-renders instead of buffering:

        - no row yet: register, stream into WORM while hashing (single pass), persist the hash
          (origin ``render``) before the commit, then complete;
        - a complete row: hash the re-render. It must equal the recorded hash (a different output for
          the same inputs is an integrity incident); nothing is written;
        - a pending row (an earlier attempt died): hash the re-render, then pin a stored version that
          holds exactly those bytes, or upload it.

        A stream that raises (evidence that fails verification, a render error) aborts the upload,
        so no object exists and the row stays pending. Nothing partial can be completed.
        """
        if "/" in name or not name:
            raise ValueError(f"unsafe production name {name!r}")
        key = production_key(tenant_id, render_id, name)
        retain = effective_retain_until(self._settings, matter_retention_until)
        existing = await self._row_by_key(tenant_id, key)
        if existing is None:
            evidence_id = new_id()
            await self._register(
                tenant_id, job_id, evidence_id, key, "production", retain, render_id
            )
            return await self._upload_production(tenant_id, evidence_id, key, retain, stream())
        if existing.kind != "production" or (existing.job_id, existing.render_id) != (
            job_id,
            render_id,
        ):
            raise EvidenceIntegrityError(f"{key}: registered for another job or kind")
        digest, size = await _hash_stream(stream())
        if existing.state == "complete":
            if (existing.sha256, existing.size_bytes) != (digest, size):
                raise EvidenceIntegrityError(
                    f"{key}: re-render gives sha256 {digest}, recorded {existing.sha256}"
                )
            return WrittenEvidence(
                existing.id, key, digest, size, existing.version_id, deduplicated=True
            )
        if existing.state != "pending":
            raise EvidenceIntegrityError(f"{key}: unexpected registry state {existing.state}")
        if existing.source_sha256 not in (None, digest):
            raise EvidenceIntegrityError(f"{key}: persisted render hash differs from re-render")
        version = await self._find_version(key, digest, size)
        if version is None:
            return await self._upload_production(
                tenant_id, existing.id, key, existing.retain_until, stream()
            )
        if existing.source_sha256 is None:
            await self._persist_source_hash(tenant_id, existing.id, digest, "render")
        await self._complete(tenant_id, existing.id, digest, size, version)
        return WrittenEvidence(existing.id, key, digest, size, version, deduplicated=False)

    async def _upload_production(
        self,
        tenant_id: uuid.UUID,
        evidence_id: uuid.UUID,
        key: str,
        retain: datetime,
        stream: AsyncIterable[bytes],
    ) -> WrittenEvidence:
        async def record_upload(upload_id: str) -> None:
            async with tenant_tx(self._sessions, tenant_id) as s:
                await s.execute(
                    text(
                        "UPDATE evidence_objects SET upload_id = :u WHERE id = :id AND state = 'pending'"
                        " AND upload_id IS NULL"
                    ),
                    {"u": upload_id, "id": evidence_id},
                )

        async def persist_render_hash(sha256: str, _size: int) -> None:
            row = await self._row_by_key(tenant_id, key)
            if row is not None and row.source_sha256 is not None:
                if row.source_sha256 != sha256:  # an earlier attempt persisted another output
                    raise EvidenceIntegrityError(
                        f"{key}: render hash differs from the persisted one"
                    )
                return
            await self._persist_source_hash(tenant_id, evidence_id, sha256, "render")

        result = await stream_upload(
            self._s3,
            bucket=self._bucket,
            key=key,
            stream=stream,
            part_size=self._settings.evidence_part_size_bytes,
            lock=Lock(retain),
            if_none_match=True,
            on_multipart_started=record_upload,
            before_commit=persist_render_hash,
        )
        if not result.version_id:
            raise EvidenceIntegrityError(f"{key}: store returned no VersionId (versioning off?)")
        await self._complete(tenant_id, evidence_id, result.sha256, result.size, result.version_id)
        return WrittenEvidence(
            evidence_id, key, result.sha256, result.size, result.version_id, deduplicated=False
        )

    # ------------------------------------------------------------------ natives (ADR 0015 §20.9)
    async def write_native(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        render_id: uuid.UUID,
        sha256: str,
        size: int,
        source_key: str,
        source_version_id: str,
        matter_retention_until: datetime,
        on: Callable[[str], Awaitable[None]] | None = None,
    ) -> WrittenEvidence:
        """Copy a collected file into the render's production as a native: SERVER-SIDE, from the pinned
        source version, never through the worker. Then ONE streaming SHA-256 read of the destination's
        pinned VersionId must give the source's recorded SHA-256 and size before the row completes.

        Serialized by the advisory content lock (MinIO ignores If-None-Match on copies). The row is
        registered first with the expected hash (origin ``render``: the bytes the render delivers). A
        complete row is reused (a retry); a pending one resumes: one stored version is verified and
        completed, none is copied (an upload an earlier attempt left open is aborted first), more
        than one is an integrity incident. ``on(point)``: crash-matrix seam (``native_parts_copied``
        before the copy completes, ``native_copied`` after it, ``native_verifying`` after the first
        chunk of the verification read)."""
        if size <= 0:
            raise ValueError(f"native {sha256}: size {size}")
        hit = on or _no_hook
        key = native_key(tenant_id, render_id, sha256)
        retain = effective_retain_until(self._settings, matter_retention_until)
        async with self._content_lock(key):
            try:
                async with asyncio.timeout(self._settings.evidence_copy_timeout_seconds):
                    return await self._native_locked(
                        tenant_id, job_id, render_id, key, sha256, size, retain,
                        source_key, source_version_id, hit,
                    )  # fmt: skip
            except TimeoutError as exc:
                raise EvidenceCopyTimeoutError(
                    f"{key}: native copy exceeded {self._settings.evidence_copy_timeout_seconds}s;"
                    " lock released"
                ) from exc

    async def _native_locked(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        render_id: uuid.UUID,
        key: str,
        sha256: str,
        size: int,
        retain: datetime,
        source_key: str,
        source_version_id: str,
        hit: Callable[[str], Awaitable[None]],
    ) -> WrittenEvidence:
        candidate = new_id()
        async with tenant_tx(self._sessions, tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO evidence_objects (id, tenant_id, job_id, render_id, storage_key, kind,"
                    " retain_until, source_sha256, source_hash_origin)"
                    " VALUES (:id, :t, :j, :r, :k, 'production', :ret, :h, 'render')"
                    " ON CONFLICT (storage_key) DO NOTHING"
                ),
                {"id": candidate, "t": tenant_id, "j": job_id, "r": render_id, "k": key,
                 "ret": retain, "h": sha256},
            )  # fmt: skip
        row = await self._row_by_key(tenant_id, key)
        if row.kind != "production" or (row.job_id, row.render_id) != (job_id, render_id):
            raise EvidenceIntegrityError(f"{key}: registered for another job, render or kind")
        if row.source_sha256 != sha256:
            raise EvidenceIntegrityError(f"{key}: registered with another hash")
        if row.state == "complete":
            if (row.sha256, row.size_bytes) != (sha256, size):
                raise EvidenceIntegrityError(f"{key}: registry {row.sha256} != {sha256}")
            return WrittenEvidence(row.id, key, sha256, size, row.version_id, deduplicated=True)
        if row.state != "pending":
            raise EvidenceIntegrityError(f"{key}: unexpected registry state {row.state}")

        versions = [v for v, marker in await self._versions(key) if not marker]
        if len(versions) > 1:
            raise EvidenceIntegrityError(f"{key}: {len(versions)} versions of one native")
        if versions:  # an earlier attempt copied it and died before completing: verify, complete
            version = versions[0]
        else:
            # an earlier attempt that died mid-copy left its upload open (recorded or not, if it
            # died before recording it): under the content lock no other copy runs, so abort all
            await self._abort_open_uploads(key, row.upload_id)
            version = await self._copy_native(
                tenant_id, row.id, key, size, row.retain_until, source_key, source_version_id, hit
            )
            await hit("native_copied")
        digest, got = await self._read_hash(key, version, hit)
        if (digest, got) != (sha256, size):
            raise EvidenceIntegrityError(
                f"{key}: version {version} reads {got} bytes sha256 {digest}, the source recorded"
                f" {size} bytes sha256 {sha256}"
            )
        await self._complete(tenant_id, row.id, sha256, size, version)
        return WrittenEvidence(row.id, key, sha256, size, version, deduplicated=False)

    async def _copy_native(
        self,
        tenant_id: uuid.UUID,
        evidence_id: uuid.UUID,
        key: str,
        size: int,
        retain_until: datetime,
        source_key: str,
        source_version_id: str,
        hit: Callable[[str], Awaitable[None]],
    ) -> str:
        """CreateMultipartUpload (COMPLIANCE lock and retain-until at create), UploadPartCopy from the
        PINNED source version in fixed ranges, CompleteMultipartUpload; any failure aborts."""
        s3 = self._s3
        created = await s3.create_multipart_upload(
            Bucket=self._bucket,
            Key=key,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=ensure_utc(retain_until),
        )
        upload_id = created["UploadId"]
        try:
            async with tenant_tx(self._sessions, tenant_id) as s:
                await s.execute(
                    text(  # write-once: a retry's upload is found by listing (_abort_open_uploads)
                        "UPDATE evidence_objects SET upload_id = :u WHERE id = :id AND state = 'pending'"
                        " AND upload_id IS NULL"
                    ),
                    {"u": upload_id, "id": evidence_id},
                )
            source: CopySourceTypeDef = {
                "Bucket": self._bucket,
                "Key": source_key,
                "VersionId": source_version_id,
            }
            parts: list[CompletedPartTypeDef] = []
            step = self._settings.evidence_copy_part_size_bytes
            for number, start in enumerate(range(0, size, step), start=1):
                end = min(start + step, size) - 1
                part = await s3.upload_part_copy(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=number,
                    CopySource=source,
                    CopySourceRange=f"bytes={start}-{end}",
                )
                parts.append({"PartNumber": number, "ETag": part["CopyPartResult"]["ETag"]})
            await hit("native_parts_copied")
            done = await s3.complete_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
                IfNoneMatch="*",
            )
        except BaseException as exc:
            try:
                await s3.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
            except Exception as abort_exc:  # noqa: BLE001 - attached to the original error
                exc.add_note(f"abort of native copy {upload_id} also failed: {abort_exc!r}")
            raise
        version = done.get("VersionId")
        if not version:
            raise EvidenceIntegrityError(f"{key}: store returned no VersionId for the copy")
        return version

    async def _abort_open_uploads(self, key: str, recorded: str | None) -> None:
        """Abort the recorded upload and every other open multipart upload at exactly ``key``."""
        ids = {recorded} if recorded else set()
        resp = await self._s3.list_multipart_uploads(Bucket=self._bucket, Prefix=key)
        ids |= {u["UploadId"] for u in resp.get("Uploads", []) if u.get("Key") == key}
        for upload_id in sorted(ids):
            try:
                await self._s3.abort_multipart_upload(
                    Bucket=self._bucket, Key=key, UploadId=upload_id
                )
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) != "NoSuchUpload":
                    raise

    async def _read_hash(
        self, key: str, version_id: str, hit: Callable[[str], Awaitable[None]]
    ) -> tuple[str, int]:
        """One streaming SHA-256 read of exactly this version (our hash, never S3's checksum)."""
        resp = await self._s3.get_object(Bucket=self._bucket, Key=key, VersionId=version_id)
        digest, size, first = hashlib.sha256(), 0, True
        async with resp["Body"] as body:
            async for chunk in body.iter_chunks(1 << 20):
                digest.update(chunk)
                size += len(chunk)
                if first:
                    first = False
                    await hit("native_verifying")
        return digest.hexdigest(), size

    async def _row_by_key(self, tenant_id: uuid.UUID, key: str) -> Any:
        async with tenant_tx(self._sessions, tenant_id) as s:
            return (
                await s.execute(
                    text(
                        "SELECT id, kind, job_id, render_id, state, sha256, size_bytes, source_sha256,"
                        " version_id, retain_until, upload_id FROM evidence_objects WHERE storage_key = :k"
                    ),
                    {"k": key},
                )
            ).one_or_none()

    # ------------------------------------------------------------------ files
    async def write_file(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        matter_retention_until: datetime,
        stream: AsyncIterable[bytes],
    ) -> WrittenEvidence:
        small_max = self._settings.evidence_small_file_max_bytes
        source = aiter(stream)
        buffered: list[bytes] = []
        if small_max > 0:
            size = 0
            async for chunk in source:
                buffered.append(chunk)
                size += len(chunk)
                if size > small_max:
                    break
            else:  # the whole file fits: small-file path
                return await self._write_small(
                    tenant_id, job_id, matter_retention_until, b"".join(buffered)
                )

        async def rest() -> AsyncIterator[bytes]:  # what was buffered, then the same source stream
            for chunk in buffered:
                yield chunk
            async for chunk in source:
                yield chunk

        return await self._write_staged(tenant_id, job_id, matter_retention_until, rest())

    async def _write_small(
        self, tenant_id: uuid.UUID, job_id: uuid.UUID, matter_retention_until: datetime, data: bytes
    ) -> WrittenEvidence:
        sha256 = hashlib.sha256(data).hexdigest()
        key = file_key(tenant_id, sha256)
        retain = effective_retain_until(self._settings, matter_retention_until)
        hit = await self._dedup_hit(tenant_id, key, sha256, len(data), retain)
        if hit is not None:
            return hit

        async def put(retain_until: datetime) -> str:
            return await self._put_small(key, data, sha256, retain_until)

        return await self._materialize(tenant_id, job_id, key, sha256, len(data), retain, put)

    async def _write_staged(
        self,
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
            key = file_key(tenant_id, staged.sha256)
            retain = effective_retain_until(self._settings, matter_retention_until)
            hit = await self._dedup_hit(tenant_id, key, staged.sha256, staged.size, retain)
            if hit is not None:
                return hit

            async def copy(retain_until: datetime) -> str:
                return await self._copy_into_worm(staging_key, key, staged, retain_until)

            return await self._materialize(
                tenant_id, job_id, key, staged.sha256, staged.size, retain, copy
            )
        finally:
            await self._s3.delete_object(Bucket=self._settings.s3_staging_bucket, Key=staging_key)

    async def lock_staged(
        self, *, tenant_id: uuid.UUID, staging_key: str, sha256: str, size: int
    ) -> WrittenEvidence:
        """Lock an object that is already in the staging bucket (an uploaded export, ADR 0014). The caller
        computed ``sha256``/``size`` by streaming the staged object; the same content-addressed key, dedup,
        provenance and verify-after-copy rules as for large files apply. The caller deletes staging.
        Not tied to a job or matter: retention is the rolling window; jobs that use it extend it."""
        key = file_key(tenant_id, sha256)
        retain = effective_retain_until(self._settings)
        hit = await self._dedup_hit(tenant_id, key, sha256, size, retain)
        if hit is not None:
            return hit
        staged = UploadResult(sha256, size, 0, None)

        async def copy(retain_until: datetime) -> str:
            return await self._copy_into_worm(staging_key, key, staged, retain_until)

        return await self._materialize(tenant_id, None, key, sha256, size, retain, copy)

    async def register_archive_entry(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID | None,
        archive_evidence_id: uuid.UUID,
        entry_path: str,
        entry_raw_name: bytes,
        entry_crc32: int,
        entry_compressed_size: int,
        sha256: str,
        size: int,
    ) -> WrittenEvidence:
        """Reference an entry INSIDE a locked archive (ADR 0014 section 2): no bytes are written. The
        caller read the entry from the archive's pinned version and computed ``sha256``/``size`` of the
        decompressed bytes (CRC-32 and size checked). The row is complete at once, pinned to the
        archive's version; the same entry read again (another job) reuses it, and must hash the same."""
        async with tenant_tx(self._sessions, tenant_id) as s:
            archive = (
                await s.execute(
                    text(
                        "SELECT storage_key, version_id, retain_until, state, kind FROM evidence_objects"
                        " WHERE id = :a"
                    ),
                    {"a": archive_evidence_id},
                )
            ).one()
            if archive.state != "complete" or archive.kind == "archive_entry":
                raise EvidenceIntegrityError(
                    f"evidence {archive_evidence_id} is not a locked archive"
                )
            key = f"{archive.storage_key}#{entry_path}"
            candidate = new_id()
            await s.execute(
                text(
                    "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, state, sha256,"
                    " size_bytes, retain_until, completed_at, version_id, source_sha256, source_hash_origin,"
                    " archive_evidence_id, entry_path, entry_raw_name, entry_crc32, entry_compressed_size)"
                    " VALUES (:id, :t, :j, :k, 'archive_entry', 'complete', :h, :n, :r, now(), :v, :h,"
                    " 'collection', :a, :p, :raw, :crc, :cs) ON CONFLICT (storage_key) DO NOTHING"
                ),
                {"id": candidate, "t": tenant_id, "j": job_id, "k": key, "h": sha256, "n": size,
                 "r": archive.retain_until, "v": archive.version_id, "a": archive_evidence_id,
                 "p": entry_path, "raw": entry_raw_name, "crc": entry_crc32, "cs": entry_compressed_size},
            )  # fmt: skip
            row = (
                await s.execute(
                    text(
                        "SELECT id, sha256, size_bytes, version_id, entry_crc32 FROM evidence_objects"
                        " WHERE storage_key = :k"
                    ),
                    {"k": key},
                )
            ).one()
        if (row.sha256, row.size_bytes, row.entry_crc32) != (sha256, size, entry_crc32):
            raise EvidenceIntegrityError(f"{key}: the entry read now differs from the one recorded")
        return WrittenEvidence(
            row.id, key, sha256, size, row.version_id, deduplicated=row.id != candidate
        )

    async def _dedup_hit(
        self, tenant_id: uuid.UUID, key: str, sha256: str, size: int, retain: datetime
    ) -> WrittenEvidence | None:
        """The content key is already complete in the registry: dedup without lock, upload or copy.
        Complete rows are final; extending retention is monotonic and idempotent."""
        async with tenant_tx(self._sessions, tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT id, state, sha256, retain_until, version_id FROM evidence_objects"
                        " WHERE storage_key = :k"
                    ),
                    {"k": key},
                )
            ).one_or_none()
        if row is None or row.state != "complete":
            return None
        if row.sha256 != sha256:
            raise EvidenceIntegrityError(f"{key}: registry sha256 != {sha256}")
        await self.extend_retention(
            tenant_id, row.id, key, row.version_id, row.retain_until, retain
        )
        return WrittenEvidence(row.id, key, sha256, size, row.version_id, deduplicated=True)

    async def _materialize(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID | None,
        key: str,
        sha256: str,
        size: int,
        retain: datetime,
        write: Callable[[datetime], Awaitable[str]],
    ) -> WrittenEvidence:
        """Create the content-addressed object once, under the per-key lock (MinIO ignores If-None-Match
        on CopyObject, so the DB advisory lock on a dedicated connection prevents a second write)."""
        async with self._content_lock(key):
            try:
                # a hung write must not hold the content lock forever
                async with asyncio.timeout(self._settings.evidence_copy_timeout_seconds):
                    return await self._materialize_locked(
                        tenant_id, job_id, key, sha256, size, retain, write
                    )
            except TimeoutError as exc:
                raise EvidenceCopyTimeoutError(
                    f"{key}: promotion exceeded {self._settings.evidence_copy_timeout_seconds}s; lock released"
                ) from exc

    async def _materialize_locked(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID | None,
        key: str,
        sha256: str,
        size: int,
        retain: datetime,
        write: Callable[[datetime], Awaitable[str]],
    ) -> WrittenEvidence:
        """Runs while the per-key advisory lock is held."""
        candidate = new_id()
        async with tenant_tx(self._sessions, tenant_id) as s:
            # the source hash is persisted with the row, BEFORE any write into WORM
            await s.execute(
                text(
                    "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind,"
                    " retain_until, source_sha256, source_hash_origin)"
                    " VALUES (:id, :t, :j, :k, 'file', :r, :h, 'collection')"
                    " ON CONFLICT (storage_key) DO NOTHING"
                ),
                {"id": candidate, "t": tenant_id, "j": job_id, "k": key, "r": retain, "h": sha256},
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
            if row.sha256 != sha256:
                raise EvidenceIntegrityError(f"{key}: registry sha256 != {sha256}")
            await self.extend_retention(
                tenant_id, row.id, key, row.version_id, row.retain_until, retain
            )
            return WrittenEvidence(row.id, key, sha256, size, row.version_id, deduplicated=True)
        if row.state != "pending":
            raise EvidenceIntegrityError(f"{key}: unexpected registry state {row.state}")
        if row.source_sha256 is None:  # left by an older writer: persist before the write
            await self._persist_source_hash(tenant_id, row.id, sha256, "collection")
        elif row.source_sha256 != sha256:
            raise EvidenceIntegrityError(f"{key}: persisted source hash differs from stream")

        version = await self._find_version(key, sha256, size)
        if version is None:
            version = await write(row.retain_until)
            if not await self._version_matches(key, version, sha256, size):
                raise EvidenceIntegrityError(f"{key}: written version {version} != source hash")
        await self._complete(tenant_id, row.id, sha256, size, version)
        return WrittenEvidence(row.id, key, sha256, size, version, deduplicated=row.id != candidate)

    async def _put_small(self, key: str, data: bytes, sha256: str, retain_until: datetime) -> str:
        """One PutObject into WORM: never overwrites (If-None-Match), locked at creation, and the store
        checks the body against our SHA-256 (it then reports that full-object checksum on HEAD)."""
        resp = await self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            IfNoneMatch="*",
            ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=ensure_utc(retain_until),
        )
        version = resp.get("VersionId")
        if not version:
            raise EvidenceIntegrityError(f"{key}: store returned no VersionId (versioning off?)")
        return version

    @asynccontextmanager
    async def _content_lock(self, key: str) -> AsyncIterator[None]:
        """Session-level advisory lock on a content key, on a DEDICATED, never-pooled connection.

        - A direct asyncpg connection opened for this lock only (not from the SQLAlchemy pool), outside
          any transaction: a long copy is never "idle in transaction", and no other work can run on it.
        - Unlocked in ``finally``; the connection is ALWAYS closed afterwards (terminated if closing
          hangs). Closing a session releases its advisory locks even if the unlock itself failed.
        - Waiters poll ``pg_try_advisory_lock`` with capped exponential backoff and jitter, up to a
          deadline (copy timeout + margin), then raise a retryable ``ContentLockTimeoutError``.
        """
        settings = self._settings
        conn = await asyncpg.connect(
            settings.pg_dsn("app"), server_settings={"application_name": "edisc-content-lock"}
        )
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + settings.evidence_copy_timeout_seconds + 60
            delay = 0.05
            while not await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key
            ):
                if loop.time() >= deadline:
                    raise ContentLockTimeoutError(
                        f"{key}: another writer held the content lock too long"
                    )
                await asyncio.sleep(delay * random.uniform(0.5, 1.0))  # noqa: S311 - jitter, not crypto
                delay = min(delay * 2, 2.0)
            try:
                yield
            finally:
                try:
                    await asyncio.wait_for(
                        conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key),
                        timeout=10,
                    )
                except (asyncpg.PostgresError, OSError, TimeoutError) as unlock_exc:
                    # closing the connection below releases the lock anyway; record, never swallow silently
                    log.warning(
                        "advisory unlock failed; closing the lock connection releases it",
                        key=key,
                        error=type(unlock_exc).__name__,
                    )
        finally:
            try:
                await asyncio.wait_for(conn.close(), timeout=10)
            except (asyncpg.PostgresError, OSError, TimeoutError):
                conn.terminate()

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
    async def extend_retention(
        self,
        tenant_id: uuid.UUID,
        evidence_id: uuid.UUID,
        key: str,
        version_id: str,
        current: datetime,
        wanted: datetime,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Extend-only, and only below the floor (``extension_needed``). True if it extended."""
        if wanted <= current or not extension_needed(self._settings, current, wanted, now=now):
            return False
        try:
            await self._s3.put_object_retention(
                Bucket=self._bucket,
                Key=key,
                VersionId=version_id,
                Retention={"Mode": "COMPLIANCE", "RetainUntilDate": ensure_utc(wanted)},
            )
        except ClientError:
            # COMPLIANCE refuses any shortening. A concurrent dedup hit (no content lock on that path)
            # may already have extended past ``wanted``: that is success. Anything else is raised.
            stored = await self._s3.get_object_retention(
                Bucket=self._bucket, Key=key, VersionId=version_id
            )
            until = stored["Retention"].get("RetainUntilDate")
            if until is None or ensure_utc(until) < ensure_utc(wanted):
                raise
            wanted = ensure_utc(until)
        async with tenant_tx(self._sessions, tenant_id) as s:
            await s.execute(
                text(
                    "UPDATE evidence_objects SET retain_until = :r WHERE id = :id AND retain_until < :r"
                ),
                {"r": wanted, "id": evidence_id},
            )
        return True

    # ------------------------------------------------------------------ registry
    async def _register(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        evidence_id: uuid.UUID,
        key: str,
        kind: str,
        retain: datetime,
        render_id: uuid.UUID | None = None,
    ) -> None:
        async with tenant_tx(self._sessions, tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO evidence_objects (id, tenant_id, job_id, render_id, storage_key, kind,"
                    " retain_until) VALUES (:id, :t, :j, :rid, :k, :kind, :r)"
                ),
                {
                    "id": evidence_id,
                    "t": tenant_id,
                    "j": job_id,
                    "rid": render_id,
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
