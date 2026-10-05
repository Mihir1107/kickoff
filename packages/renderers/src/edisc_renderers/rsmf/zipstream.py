"""Deterministic, streaming `rsmf.zip` (ADR 0015 §6, and §20.11 from renderer 1.3.0).

The bytes come from `edisc_custody.zipwriter`, the same writer as the render packages:

- Entries in name order, every timestamp 1980-01-01 00:00, Unix mode 0644, UTF-8 names (flag bit 11),
  no comments.
- STORED, not deflated. Deflate output depends on the zlib build (zlib, zlib-ng and their versions
  produce different bytes), so it cannot be byte-identical across machines.
- EVERY entry carries its CRC-32 and sizes in a data descriptor (flag bit 3), computed as the bytes
  pass: in-memory entries (the manifest, placeholders) and streamed evidence follow one rule.
  Evidence bytes are checked against the recorded size and SHA-256 as they pass, and a mismatch raises.
- No ZIP64: the renderer moves attachments out of the zip until `ZipSizer.needs_zip64()` is false
  (§20.11), so a zip that would still need it raises before any byte is produced.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass

from edisc_custody.zipwriter import ZipMember, ZipSizer, zip_stream
from edisc_renderers.rsmf.model import (
    AsyncFileOpener,
    EvidenceMismatchError,
    FileAttachment,
    FileOpener,
    ZipLimitError,
)


@dataclass(frozen=True)
class ZipEntry:
    name: str
    data: bytes | None = None  # in-memory content
    file: FileAttachment | None = None  # streamed evidence

    @property
    def size(self) -> int:
        if self.data is not None:
            return len(self.data)
        if self.file is None:
            raise ValueError("zip entry without content")
        return self.file.size


def sizer(entries: Sequence[ZipEntry]) -> ZipSizer:
    """The writer's own sizing of `entries`, in the order given (no content needed)."""
    s = ZipSizer()
    for e in entries:
        s.add(e.name, e.size)
    return s


def zip_size(entries: Sequence[ZipEntry]) -> int:
    """Exact size of the zip `azip_stream` produces for `entries`."""
    return sizer(entries).total()


def check_limits(entries: Sequence[ZipEntry]) -> None:
    names = [e.name for e in entries]
    if names != sorted(set(names), key=lambda n: n.encode("utf-8")):
        raise ValueError("zip entries must be unique and sorted by name")
    s = sizer(entries)
    if s.needs_zip64():
        raise ZipLimitError(f"zip of {s.count} entries and {s.total()} bytes needs ZIP64")


async def _verified(file: FileAttachment, opener: AsyncFileOpener) -> AsyncIterator[bytes]:
    """A file's chunks; size and SHA-256 are checked against the record as they pass."""
    size, digest = 0, hashlib.sha256()
    async for chunk in opener(file):
        if not chunk:
            continue
        size += len(chunk)
        digest.update(chunk)
        if size > file.size:
            raise EvidenceMismatchError(
                f"file {file.file_id}: more bytes than the recorded {file.size}"
            )
        yield chunk
    if size != file.size or digest.hexdigest() != file.sha256:
        raise EvidenceMismatchError(
            f"file {file.file_id}: streamed {size} bytes sha256 {digest.hexdigest()}, "
            f"recorded {file.size} bytes sha256 {file.sha256}"
        )


def _member(e: ZipEntry, opener: AsyncFileOpener) -> ZipMember:
    if e.data is not None:
        data = e.data

        async def held() -> AsyncIterator[bytes]:
            yield data

        return ZipMember(e.name, len(data), held)
    if e.file is not None:
        file = e.file
        return ZipMember(e.name, file.size, lambda: _verified(file, opener))
    raise ValueError(f"zip entry {e.name} has no content")


async def azip_stream(entries: Sequence[ZipEntry], opener: AsyncFileOpener) -> AsyncIterator[bytes]:
    """The zip as an async stream: evidence is read through `opener` one chunk at a time."""
    check_limits(entries)

    async def members() -> AsyncIterator[ZipMember]:
        for e in entries:
            yield _member(e, opener)

    async for chunk in zip_stream(members()):
        yield chunk


def as_async(opener: FileOpener) -> AsyncFileOpener:
    """A synchronous opener (tests, in-memory data) as an async one."""

    async def aopen(file: FileAttachment) -> AsyncIterator[bytes]:
        for chunk in opener(file):
            yield chunk

    return aopen


def drive(stream: AsyncIterator[bytes]) -> Iterator[bytes]:
    """Iterate an async byte stream from synchronous code (outside any running event loop), lazily:
    one chunk at a time on a private loop, so memory stays bounded."""
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                yield loop.run_until_complete(anext(stream))
            except StopAsyncIteration:
                return
    finally:
        loop.run_until_complete(stream.aclose())  # type: ignore[attr-defined]
        loop.close()
