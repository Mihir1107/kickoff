"""Random access to a pinned evidence object for the ZIP reader (ADR 0014 R6).

``S3ObjectSource`` reads byte ranges of one pinned object version (never "latest"). ``CoalescingSource``
turns the reader's many small sequential reads (headers, 1 MiB data chunks) into few large range
requests: a read outside the cached window fetches a new window of ``window`` bytes starting at the read.
Reading entries in local-header order therefore costs about one request per ``window`` bytes of archive,
not one per entry. ``requests`` counts what was actually sent (measured in docs/runs).
"""

from __future__ import annotations

from types_aiobotocore_s3 import S3Client


class S3ObjectSource:
    def __init__(self, s3: S3Client, *, bucket: str, key: str, version_id: str, size: int) -> None:
        self._s3, self._bucket, self._key, self._version = s3, bucket, key, version_id
        self.size = size
        self.requests = 0

    async def read(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset >= self.size:
            return b""
        last = min(offset + length, self.size) - 1
        self.requests += 1
        resp = await self._s3.get_object(
            Bucket=self._bucket,
            Key=self._key,
            VersionId=self._version,
            Range=f"bytes={offset}-{last}",
        )
        async with resp["Body"] as body:
            data: bytes = await body.read()
        return data


class CoalescingSource:
    def __init__(self, inner: S3ObjectSource, *, window: int = 8 << 20) -> None:
        self._inner, self._window = inner, window
        self.size = inner.size
        self._start, self._data = 0, b""

    @property
    def requests(self) -> int:
        return self._inner.requests

    async def read(self, offset: int, length: int) -> bytes:
        end = offset + length
        if self._start <= offset and end <= self._start + len(self._data):
            return self._data[offset - self._start : end - self._start]
        fetch = max(self._window, length)
        self._data = await self._inner.read(offset, fetch)
        self._start = offset
        return self._data[:length]
