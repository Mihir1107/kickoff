"""Streaming upload with our own full-object SHA-256 (the evidence hash) and per-part SHA-256 checksums
(transport integrity only: S3 multipart checksums are composite and never used as the evidence hash).

Memory: at most one part buffer (``part_size``) plus one incoming chunk.

Objects that fit in one part (including 0 bytes) go up as a single PutObject. Larger ones use multipart
upload with Object Lock mode and retain-until set at CreateMultipartUpload, so a completed object is
never unlocked, even for a moment. Any failure or cancellation aborts the multipart upload.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from types_aiobotocore_s3 import S3Client
from types_aiobotocore_s3.type_defs import CompletedPartTypeDef

from edisc_core.time import ensure_utc


@dataclass(frozen=True)
class UploadResult:
    sha256: str  # hex, full object: the evidence hash
    size: int
    parts: int  # 0 for single PutObject
    version_id: str | None


@dataclass(frozen=True)
class Lock:
    retain_until: datetime
    mode: str = "COMPLIANCE"


def _b64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


async def _parts(stream: AsyncIterable[bytes], part_size: int) -> AsyncIterator[bytes]:
    """Re-chunk ``stream`` into ``part_size`` parts; the last part may be shorter (or empty if no data)."""
    buf = bytearray()
    emitted = False
    async for chunk in stream:
        if not chunk:
            continue
        buf += chunk
        while len(buf) >= part_size:
            with memoryview(buf) as view:
                part = bytes(view[:part_size])  # one copy, not slice-then-copy
            del buf[:part_size]
            emitted = True
            yield part
    if buf or not emitted:
        yield bytes(buf)


async def stream_upload(
    client: S3Client,
    *,
    bucket: str,
    key: str,
    stream: AsyncIterable[bytes],
    part_size: int,
    lock: Lock | None,
    if_none_match: bool,
    on_multipart_started: Callable[[str], Awaitable[None]] | None = None,
) -> UploadResult:
    """Upload ``stream`` to ``bucket/key`` while hashing. Never overwrites when ``if_none_match``."""
    full = hashlib.sha256()
    size = 0
    parts_iter = _parts(stream, part_size)
    first = await anext(parts_iter)
    second = await anext(parts_iter, None)
    lock_args: dict[str, Any] = (
        {"ObjectLockMode": lock.mode, "ObjectLockRetainUntilDate": ensure_utc(lock.retain_until)}
        if lock
        else {}
    )
    cond_args: dict[str, Any] = {"IfNoneMatch": "*"} if if_none_match else {}

    if second is None:  # fits in one part: single atomic PUT
        full.update(first)
        put_resp = await client.put_object(
            Bucket=bucket,
            Key=key,
            Body=first,
            ChecksumSHA256=_b64(hashlib.sha256(first).digest()),
            **lock_args,
            **cond_args,
        )
        return UploadResult(full.hexdigest(), len(first), 0, put_resp.get("VersionId"))

    created = await client.create_multipart_upload(
        Bucket=bucket, Key=key, ChecksumAlgorithm="SHA256", **lock_args
    )
    upload_id = created["UploadId"]
    completed: list[CompletedPartTypeDef] = []
    try:
        if on_multipart_started is not None:
            await on_multipart_started(upload_id)

        held: list[bytes] = [first, second]  # released as they are uploaded

        async def pending() -> AsyncIterator[bytes]:
            while held:
                yield held.pop(0)
            async for p in parts_iter:
                yield p

        del first, second
        number = 0
        async for part in pending():
            if not part:
                continue
            number += 1
            full.update(part)
            size += len(part)
            part_resp = await client.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=number,
                Body=part,
                ChecksumSHA256=_b64(hashlib.sha256(part).digest()),
            )
            completed.append(
                {
                    "PartNumber": number,
                    "ETag": part_resp["ETag"],
                    "ChecksumSHA256": part_resp["ChecksumSHA256"],
                }
            )
        done = await client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": completed},
            **cond_args,
        )
    except BaseException as exc:
        # Shield the abort from cancellation so a cancelled activity still cleans up; then re-raise the
        # ORIGINAL error. If the abort itself fails, that is attached to it, never swallowed.
        try:
            await asyncio.shield(_abort(client, bucket, key, upload_id))
        except Exception as abort_exc:  # noqa: BLE001 - recorded on the original exception below
            exc.add_note(
                f"abort of multipart upload {upload_id} for {key} also failed: {abort_exc!r}"
            )
        raise
    return UploadResult(full.hexdigest(), size, len(completed), done.get("VersionId"))


async def _abort(client: S3Client, bucket: str, key: str, upload_id: str) -> None:
    await client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)


async def rehash_object(
    client: S3Client, *, bucket: str, key: str, version_id: str | None = None, chunk: int = 1 << 20
) -> tuple[str, int]:
    """Stream an object back and return (sha256 hex, size). Bounded memory."""
    kwargs: dict[str, Any] = {"VersionId": version_id} if version_id else {}
    resp = await client.get_object(Bucket=bucket, Key=key, **kwargs)
    h, size = hashlib.sha256(), 0
    async with resp["Body"] as body:
        async for data in body.iter_chunks(chunk):
            h.update(data)
            size += len(data)
    return h.hexdigest(), size
