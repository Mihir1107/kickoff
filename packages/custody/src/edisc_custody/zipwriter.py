"""Deterministic streaming ZIP writer for packages (ADR 0015 §18.5 and §19). Pure: standard library only.

The same members in the same order always give the same bytes:

- STORED only (deflate bytes depend on the zlib build); every timestamp 1980-01-01 00:00; Unix mode
  0644; UTF-8 names (flag bit 11); no comments; no extra fields except ZIP64 where it is needed.
- EVERY entry is streamed once: its CRC-32 is computed as the bytes pass and written, with the sizes,
  in a data descriptor (flag bit 3). Nothing about the content is needed before its first byte, so
  older renders (with no CRC recorded) and new ones follow one rule.
- ZIP64 when it is needed, decided from the DECLARED size and the position, so the decision never
  depends on the content: an entry of 0xFFFFFFFF bytes or more gets a ZIP64 extra field in its local
  header (sizes 0xFFFFFFFF there, zeros in the extra) and a 64-bit data descriptor; a central record
  gets a ZIP64 extra for each size or offset that does not fit; the archive gets a ZIP64 end record
  and locator when the entry count, directory size or directory offset does not fit.
- A member must produce exactly its declared size: one byte more raises before that chunk is
  passed on, fewer raises at its end (``ZipSizeError``). Verifying the CONTENT (hashes) is the
  caller's job, inside the member's chunk iterator.

Central directory records are kept in memory until the end (about 100 bytes per entry).
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass

_LOCAL = struct.Struct("<IHHHHHIIIHH")
_CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")
_DESCRIPTOR = struct.Struct("<IIII")
_DESCRIPTOR64 = struct.Struct("<IIQQ")
_EOCD = struct.Struct("<IHHHHIIH")
_EOCD64 = struct.Struct("<IQHHIIQQQQ")
_LOCATOR64 = struct.Struct("<IIQI")
_LOCAL_SIG, _CENTRAL_SIG, _DESCRIPTOR_SIG = 0x04034B50, 0x02014B50, 0x08074B50
_EOCD_SIG, _EOCD64_SIG, _LOCATOR64_SIG = 0x06054B50, 0x06064B50, 0x07064B50
_ZIP64_TAG = 0x0001
_VERSION, _VERSION64 = 20, 45  # 2.0; 4.5 = ZIP64
_UNIX = 3 << 8
_FLAGS = 0x0800 | 0x0008  # UTF-8 names, data descriptor
_DOS_TIME = 0
_DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01
_EXTERNAL_ATTR = 0o100644 << 16
MAX32 = 0xFFFFFFFF
MAX16 = 0xFFFF


class ZipSizeError(ValueError):
    """A member produced more or fewer bytes than it declared."""


@dataclass(frozen=True)
class ZipMember:
    name: str
    size: int  # declared: the stream must produce exactly this many bytes
    chunks: Callable[[], AsyncIterator[bytes]]  # opened only when the writer reaches the entry


def needs_zip64(size: int) -> bool:
    return size >= MAX32


async def zip_stream(members: AsyncIterable[ZipMember]) -> AsyncIterator[bytes]:
    """The archive, one chunk at a time. Members are written in the order given; names must be
    unique. Data chunks are passed through unchanged (never copied or buffered)."""
    offset = 0
    central: list[bytes] = []
    seen: set[str] = set()
    count = 0
    async for m in members:
        if m.name in seen:
            raise ValueError(f"duplicate zip entry {m.name!r}")
        if m.size < 0:
            raise ValueError(f"zip entry {m.name!r}: negative size")
        seen.add(m.name)
        name = m.name.encode("utf-8")
        big = needs_zip64(m.size)
        header = _local_header(name, m.size)
        yield header
        crc, size = 0, 0
        async for chunk in m.chunks():
            if not chunk:
                continue
            size += len(chunk)
            if size > m.size:
                raise ZipSizeError(f"zip entry {m.name!r}: more than the declared {m.size} bytes")
            crc = zlib.crc32(chunk, crc)
            yield chunk
        if size != m.size:
            raise ZipSizeError(f"zip entry {m.name!r}: {size} bytes, declared {m.size}")
        descriptor = (
            _DESCRIPTOR64.pack(_DESCRIPTOR_SIG, crc, size, size)
            if big
            else _DESCRIPTOR.pack(_DESCRIPTOR_SIG, crc, size, size)
        )
        yield descriptor
        central.append(_central_record(name, crc, size, offset))
        offset += len(header) + size + len(descriptor)
        count += 1
    directory = b"".join(central)
    yield directory
    yield _end_records(count, len(directory), offset)


def _local_header(name: bytes, size: int) -> bytes:
    big = needs_zip64(size)
    extra = struct.pack("<HHQQ", _ZIP64_TAG, 16, 0, 0) if big else b""
    header = _LOCAL.pack(
        _LOCAL_SIG, _VERSION64 if big else _VERSION, _FLAGS, 0, _DOS_TIME, _DOS_DATE,
        0, MAX32 if big else 0, MAX32 if big else 0, len(name), len(extra),
    )  # fmt: skip
    return header + name + extra


class ZipSizer:
    """The exact size of the archive ``zip_stream`` writes for the same names and declared sizes in
    the same order, before any content exists (the download's Content-Length). Built from the
    writer's own record builders, so the two cannot drift apart."""

    def __init__(self) -> None:
        self.offset = self.cd_size = self.count = 0

    def add(self, name: str, size: int) -> None:
        raw = name.encode("utf-8")
        self.cd_size += len(_central_record(raw, 0, size, self.offset))
        descriptor = _DESCRIPTOR64.size if needs_zip64(size) else _DESCRIPTOR.size
        self.offset += len(_local_header(raw, size)) + size + descriptor
        self.count += 1

    def total(self) -> int:
        end = _end_records(self.count, self.cd_size, self.offset)
        return self.offset + self.cd_size + len(end)


def _central_record(name: bytes, crc: int, size: int, offset: int) -> bytes:
    values = [size, size] if needs_zip64(size) else []  # uncompressed, compressed (APPNOTE order)
    if offset >= MAX32:
        values.append(offset)
    extra = (
        struct.pack(f"<HH{len(values)}Q", _ZIP64_TAG, 8 * len(values), *values) if values else b""
    )
    version = _VERSION64 if values else _VERSION
    small = MAX32 if needs_zip64(size) else size
    record = _CENTRAL.pack(
        _CENTRAL_SIG, _UNIX | version, version, _FLAGS, 0, _DOS_TIME, _DOS_DATE, crc,
        small, small, len(name), len(extra), 0, 0, 0, _EXTERNAL_ATTR, min(offset, MAX32),
    )  # fmt: skip
    return record + name + extra


def _end_records(count: int, cd_size: int, cd_offset: int) -> bytes:
    out = b""
    if count >= MAX16 or cd_size >= MAX32 or cd_offset >= MAX32:
        record_at = cd_offset + cd_size
        out += _EOCD64.pack(
            _EOCD64_SIG, _EOCD64.size - 12, _UNIX | _VERSION64, _VERSION64, 0, 0,
            count, count, cd_size, cd_offset,
        )  # fmt: skip
        out += _LOCATOR64.pack(_LOCATOR64_SIG, 0, record_at, 1)
    n = min(count, MAX16)
    return out + _EOCD.pack(_EOCD_SIG, 0, 0, n, n, min(cd_size, MAX32), min(cd_offset, MAX32), 0)
