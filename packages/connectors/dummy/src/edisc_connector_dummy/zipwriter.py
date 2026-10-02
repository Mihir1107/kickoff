"""A small streaming ZIP writer for synthetic exports (ADR 0014 M14.4). Test-data generator only.

``zipfile`` cannot produce several real-world shapes on purpose, so this writer can:

- ``force_zip64``: ZIP64 records on every entry (and automatically past 65,535 entries or 4 GiB);
- ``data_descriptors``: general-purpose bit 3, sizes and CRC after the data (streaming zippers);
- name encodings: UTF-8 with the flag, UTF-8 WITHOUT the flag (macOS Archive Utility), CP437 (the ZIP
  default), or a CP437/ASCII header name with an Info-ZIP Unicode Path extra field (0x7075).

Output is deterministic (fixed timestamps) and written in one pass to any binary stream; only the
central-directory records are kept in memory.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import BinaryIO, Literal

STORED, DEFLATED = 0, 8
NameMode = Literal["utf8_flag", "utf8_noflag", "cp437", "unicode_extra"]
_MAX32, _MAX16 = 0xFFFFFFFF, 0xFFFF


@dataclass(frozen=True)
class _Record:
    raw_name: bytes
    flags: int
    method: int
    crc: int
    csize: int
    usize: int
    offset: int
    extra: bytes
    external_attr: int
    zip64: bool


def _dos(dt: tuple[int, int, int, int, int, int]) -> tuple[int, int]:
    y, mo, d, h, mi, s = dt
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


class ZipWriter:
    def __init__(
        self,
        out: BinaryIO,
        *,
        force_zip64: bool = False,
        data_descriptors: bool = False,
        names: NameMode = "utf8_flag",
        mtime: tuple[int, int, int, int, int, int] = (2026, 1, 5, 0, 0, 0),
    ) -> None:
        self._out, self._pos = out, 0
        self.force_zip64, self.data_descriptors, self.names = force_zip64, data_descriptors, names
        self._time, self._date = _dos(mtime)
        self._records: list[_Record] = []

    def _write(self, data: bytes) -> None:
        self._out.write(data)
        self._pos += len(data)

    def _name(self, name: str) -> tuple[bytes, int, bytes]:
        """(header name bytes, flag bits, extra field) for the configured name mode."""
        if name.isascii():
            return name.encode("ascii"), 0, b""
        if self.names == "utf8_flag":
            return name.encode("utf-8"), 0x800, b""
        if self.names == "utf8_noflag":
            return name.encode("utf-8"), 0, b""
        if self.names == "cp437":
            return name.encode("cp437"), 0, b""
        header = name.encode("ascii", "replace")  # lossy "caf?", as 7-Zip and others write
        body = struct.pack("<BI", 1, zlib.crc32(header)) + name.encode("utf-8")
        return header, 0, struct.pack("<HH", 0x7075, len(body)) + body

    def add(self, name: str, data: bytes = b"", *, method: int = DEFLATED) -> None:
        is_dir = name.endswith("/")
        if is_dir:
            method, data = STORED, b""
        raw, flags, extra = self._name(name)
        crc, usize = zlib.crc32(data), len(data)
        if method == DEFLATED:
            comp = zlib.compressobj(6, zlib.DEFLATED, -15)
            payload = comp.compress(data) + comp.flush()
        else:
            payload = data
        csize = len(payload)
        offset = self._pos
        zip64 = self.force_zip64 or max(usize, csize, offset) >= _MAX32
        if self.data_descriptors and not is_dir:
            flags |= 0x8
        local_extra = extra
        if zip64:
            local_extra = (
                struct.pack(
                    "<HHQQ", 1, 16, usize if not flags & 0x8 else 0, csize if not flags & 0x8 else 0
                )
                + extra
            )
            l_crc, l_c, l_u = (0 if flags & 0x8 else crc), _MAX32, _MAX32
        elif flags & 0x8:
            l_crc, l_c, l_u = 0, 0, 0
        else:
            l_crc, l_c, l_u = crc, csize, usize
        self._write(
            struct.pack(
                "<IHHHHHIIIHH",
                0x04034B50,
                45 if zip64 else 20,
                flags,
                method,
                self._time,
                self._date,
                l_crc,
                l_c,
                l_u,
                len(raw),
                len(local_extra),
            )
            + raw
            + local_extra
        )
        self._write(payload)
        if flags & 0x8:
            if zip64:
                self._write(struct.pack("<IIQQ", 0x08074B50, crc, csize, usize))
            else:
                self._write(struct.pack("<IIII", 0x08074B50, crc, csize, usize))
        attr = (0o40755 << 16) | 0x10 if is_dir else 0o100644 << 16
        self._records.append(
            _Record(raw, flags, method, crc, csize, usize, offset, extra, attr, zip64)
        )

    def close(self) -> None:
        cd_start = self._pos
        for r in self._records:
            extra = r.extra
            usize, csize, offset = r.usize, r.csize, r.offset
            if r.zip64:
                extra = struct.pack("<HHQQQ", 1, 24, usize, csize, offset) + extra
                usize = csize = offset = _MAX32
            self._write(
                struct.pack(
                    "<IHHHHHHIIIHHHHHII",
                    0x02014B50,
                    (3 << 8) | 45,
                    45 if r.zip64 else 20,
                    r.flags,
                    r.method,
                    self._time,
                    self._date,
                    r.crc,
                    csize,
                    usize,
                    len(r.raw_name),
                    len(extra),
                    0,
                    0,
                    0,
                    r.external_attr,
                    offset,
                )
                + r.raw_name
                + extra
            )
        cd_size = self._pos - cd_start
        n = len(self._records)
        if self.force_zip64 or n >= _MAX16 or cd_start >= _MAX32 or cd_size >= _MAX32:
            z64 = self._pos
            self._write(
                struct.pack(
                    "<IQHHIIQQQQ", 0x06064B50, 44, (3 << 8) | 45, 45, 0, 0, n, n, cd_size, cd_start
                )
            )
            self._write(struct.pack("<IIQI", 0x07064B50, 0, z64, 1))
            self._write(
                struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, _MAX16, _MAX16, _MAX32, _MAX32, 0)
            )
        else:
            self._write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, cd_size, cd_start, 0))
