"""Small immutable objects (anchors, seals, manifests) in the Object Lock bucket.

Every write: COMPLIANCE retention, ``If-None-Match: *`` (never overwrite), server-verified SHA-256.
Streaming multipart for large raw pages/files lives in the evidence writer (M6).
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from botocore.exceptions import ClientError
from types_aiobotocore_s3 import S3Client

from edisc_core.time import ensure_utc


class WormConflictError(RuntimeError):
    """The key already exists with DIFFERENT content. Never overwritten; always an integrity incident."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    version_id: str | None
    sha256: str
    size: int
    created: bool  # False when an identical object already existed (idempotent retry)


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


async def put_immutable(
    client: S3Client, *, bucket: str, key: str, body: bytes, retain_until: datetime
) -> StoredObject:
    """Write ``body`` once under COMPLIANCE lock. Retrying with identical bytes is a no-op success."""
    digest = hashlib.sha256(body)
    try:
        resp = await client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ChecksumSHA256=base64.b64encode(digest.digest()).decode(),
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=ensure_utc(retain_until),
            IfNoneMatch="*",
        )
    except ClientError as exc:
        if _error_code(exc) not in {"PreconditionFailed", "412"}:
            raise
        existing = await get_bytes(client, bucket=bucket, key=key)
        if hashlib.sha256(existing).hexdigest() != digest.hexdigest():
            raise WormConflictError(f"{key} already exists with different content") from exc
        head = await client.head_object(Bucket=bucket, Key=key)
        return StoredObject(
            key, head.get("VersionId"), digest.hexdigest(), len(body), created=False
        )
    return StoredObject(key, resp.get("VersionId"), digest.hexdigest(), len(body), created=True)


async def get_bytes(
    client: S3Client, *, bucket: str, key: str, version_id: str | None = None
) -> bytes:
    if version_id:
        resp = await client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    else:
        resp = await client.get_object(Bucket=bucket, Key=key)
    async with resp["Body"] as stream:
        data: bytes = await stream.read()
    return data


@dataclass(frozen=True)
class ObjectVersion:
    key: str
    version_id: str
    is_delete_marker: bool


async def list_versions(
    client: S3Client, *, bucket: str, prefix: str
) -> AsyncIterator[ObjectVersion]:
    """Every version and delete marker under ``prefix``, oldest first per key.

    Verifiers must use this, not ``list_objects_v2``: on a versioned bucket a delete marker hides an
    object from plain listings, and a newer PUT without ``If-None-Match`` shadows the original. Locked
    versions can be hidden or shadowed, but never removed, so reading all versions defeats both.
    """
    paginator = client.get_paginator("list_object_versions")
    async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        entries = [
            (v["Key"], v["LastModified"], v["VersionId"], False) for v in page.get("Versions", [])
        ]
        entries += [
            (m["Key"], m["LastModified"], m["VersionId"], True)
            for m in page.get("DeleteMarkers", [])
        ]
        for key, _, version_id, marker in sorted(entries, key=lambda e: (e[0], e[1])):
            yield ObjectVersion(key, version_id, is_delete_marker=marker)
