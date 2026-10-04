"""Deterministic, streaming `rsmf.zip` writer (ADR 0015 §6).

- Entries in name order, every timestamp 1980-01-01 00:00, Unix mode 0644, UTF-8 names (flag bit 11),
  no extra fields, no comments.
- STORED, not deflated. Deflate output depends on the zlib build (zlib, zlib-ng and their versions
  produce different bytes), so it cannot be byte-identical across machines. Byte identity is the
  stronger promise. This is a deviation from §6 recorded for review.
- In-memory entries (the manifest, placeholders) carry their CRC and sizes in the local header.
  Evidence files are streamed once, so their CRC and sizes follow in a data descriptor (flag bit 3).
  The bytes are checked against the recorded size and SHA-256 as they pass, and a mismatch raises.
- No ZIP64: over 4 GiB or 65,535 entries raises before any byte is produced.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import zlib
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass

from edisc_renderers.rsmf.model import (
    AsyncFileOpener,
    EvidenceMismatchError,
    FileAttachment,
    FileOpener,
    ZipLimitError,
)

_LOCAL = struct.Struct("<IHHHHHIIIHH")
_CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")
_DESCRIPTOR = struct.Struct("<IIII")
_EOCD = struct.Struct("<IHHHHIIH")
_VERSION_NEEDED = 20
_VERSION_MADE_BY = (3 << 8) | 20  # Unix, spec 2.0
_FLAG_UTF8 = 0x0800
_FLAG_DESCRIPTOR = 0x0008
_DOS_TIME = 0
_DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01
_EXTERNAL_ATTR = 0o100644 << 16
_MAX32 = 0xFFFFFFFF
_MAX_ENTRIES = 0xFFFF


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


def zip_size(entries: Sequence[ZipEntry]) -> int:
    """Exact size of the zip `zip_stream` produces for `entries`."""
    total = _EOCD.size
    for e in entries:
        name = len(e.name.encode("utf-8"))
        total += _LOCAL.size + name + e.size + _CENTRAL.size + name
        if e.file is not None:
            total += _DESCRIPTOR.size
    return total


def check_limits(entries: Sequence[ZipEntry]) -> None:
    names = [e.name for e in entries]
    if names != sorted(set(names)):
        raise ValueError("zip entries must be unique and sorted by name")
    if len(entries) > _MAX_ENTRIES or zip_size(entries) > _MAX32:
        raise ZipLimitError(
            f"zip of {len(entries)} entries and {zip_size(entries)} bytes needs ZIP64"
        )


async def _streamed(
    file: FileAttachment, opener: AsyncFileOpener
) -> AsyncIterator[tuple[bytes, int, int]]:
    """Yield chunks of a file with the running CRC-32 and size; verify size and SHA-256 at the end."""
    crc, size, digest = 0, 0, hashlib.sha256()
    async for chunk in opener(file):
        if not chunk:
            continue
        crc = zlib.crc32(chunk, crc)
        size += len(chunk)
        digest.update(chunk)
        if size > file.size:
            raise EvidenceMismatchError(
                f"file {file.file_id}: more bytes than the recorded {file.size}"
            )
        yield chunk, crc, size
    if size != file.size or digest.hexdigest() != file.sha256:
        raise EvidenceMismatchError(
            f"file {file.file_id}: streamed {size} bytes sha256 {digest.hexdigest()}, "
            f"recorded {file.size} bytes sha256 {file.sha256}"
        )


async def azip_stream(entries: Sequence[ZipEntry], opener: AsyncFileOpener) -> AsyncIterator[bytes]:
    """The zip as an async stream: evidence is read through `opener` one chunk at a time."""
    check_limits(entries)
    offset = 0
    central: list[bytes] = []
    for e in entries:
        name = e.name.encode("utf-8")
        if e.data is not None:
            crc, size, flags = zlib.crc32(e.data), len(e.data), _FLAG_UTF8
            header = _LOCAL.pack(
                0x04034B50, _VERSION_NEEDED, flags, 0, _DOS_TIME, _DOS_DATE,
                crc, size, size, len(name), 0,
            )  # fmt: skip
            yield header + name
            yield e.data
            written = len(header) + len(name) + size
        elif e.file is not None:
            flags = _FLAG_UTF8 | _FLAG_DESCRIPTOR
            header = _LOCAL.pack(
                0x04034B50, _VERSION_NEEDED, flags, 0, _DOS_TIME, _DOS_DATE, 0, 0, 0, len(name), 0
            )
            yield header + name
            crc, size = 0, 0
            async for chunk, crc, size in _streamed(e.file, opener):  # noqa: B007 (last values used below)
                yield chunk
            descriptor = _DESCRIPTOR.pack(0x08074B50, crc, size, size)
            yield descriptor
            written = len(header) + len(name) + size + len(descriptor)
        else:
            raise ValueError(f"zip entry {e.name} has no content")
        central.append(
            _CENTRAL.pack(
                0x02014B50,
                _VERSION_MADE_BY,
                _VERSION_NEEDED,
                flags,
                0,
                _DOS_TIME,
                _DOS_DATE,
                crc,
                size,
                size,
                len(name),
                0,
                0,
                0,
                0,
                _EXTERNAL_ATTR,
                offset,
            )
            + name
        )
        offset += written
    directory = b"".join(central)
    yield directory
    yield _EOCD.pack(0x06054B50, 0, 0, len(entries), len(entries), len(directory), offset, 0)


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
