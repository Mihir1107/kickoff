"""Hardened streaming ZIP reader (ADR 0014 section 3). Pure: standard library only, no DB or cloud imports,
so the offline verifier (``edisc-verify``) and the collection worker parse archives identically.

The archive is attacker-influenced. Nothing declared in it is trusted:

- the central directory is parsed as a stream in bounded chunks (``iter_central_directory``), never
  materialized; every record is validated as it is read (names, methods, flags, ZIP64 fields, offsets);
- entry data is read through ``open_entry``: the local header must match the central directory, the data
  must lie between its header and the central directory, and decompression is streamed in bounded
  chunks, stopping one byte past the declared size;
- CRC-32 and size are checked on every read; SHA-256 of the decompressed bytes is computed while reading.

Every failure is an ``ArchiveError`` with a classified ``code``. No other exception escapes for malformed
input (fuzz-tested). Duplicate-name detection is the caller's job (``fold_name``): a 20-million-entry
directory cannot be held in memory, so the worker enforces it with a database unique key.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import struct
import unicodedata
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

EOCD_SIG = 0x06054B50
ZIP64_LOCATOR_SIG = 0x07064B50
ZIP64_EOCD_SIG = 0x06064B50
CD_SIG = 0x02014B50
LOCAL_SIG = 0x04034B50

EOCD_LEN = 22
ZIP64_LOCATOR_LEN = 20
ZIP64_EOCD_MIN = 56
CD_LEN = 46
LOCAL_LEN = 30
MAX_COMMENT = 0xFFFF
STORED, DEFLATED = 0, 8
_DRIVE = re.compile(r"^[A-Za-z]:")


class ArchiveErrorCode(StrEnum):
    TRUNCATED = "truncated"
    BAD_EOCD = "bad_eocd"
    BAD_ZIP64 = "bad_zip64"
    MULTIDISK = "multidisk"
    BAD_CENTRAL_DIRECTORY = "bad_central_directory"
    TOO_MANY_ENTRIES = "too_many_entries"
    ENTRY_TOO_LARGE = "entry_too_large"
    TOTAL_TOO_LARGE = "total_too_large"
    RATIO = "compression_ratio"
    METHOD = "unsupported_method"
    ENCRYPTED = "encrypted"
    BAD_NAME = "bad_name"
    SYMLINK = "symlink"
    DUPLICATE_NAME = "duplicate_name"
    OUT_OF_BOUNDS = "out_of_bounds"
    OVERLAP = "overlap"
    HEADER_MISMATCH = "header_mismatch"
    CRC_MISMATCH = "crc_mismatch"
    SIZE_MISMATCH = "size_mismatch"
    CORRUPT_DATA = "corrupt_data"


class ArchiveError(Exception):
    def __init__(self, code: ArchiveErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code, self.detail = code, detail


def _fail(code: ArchiveErrorCode, detail: str) -> ArchiveError:
    return ArchiveError(code, detail)


@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = 200 * 10**9
    max_entries: int = 20_000_000
    max_entry_bytes: int = 1 << 30
    max_total_bytes: int = 2 * 10**12
    max_total_ratio: int = (
        100  # total decompressed <= ratio x archive size (and <= max_total_bytes)
    )
    max_entry_ratio: int = 200
    ratio_floor_bytes: int = 1 << 20  # the per-entry ratio applies above this decompressed size
    max_name_bytes: int = 1024
    read_chunk: int = 1 << 20

    def total_cap(self, archive_size: int) -> int:
        return min(self.max_total_bytes, self.max_total_ratio * max(archive_size, 1))


class Source(Protocol):
    """Random access to the archive bytes (a pinned S3 object version, a local file, memory)."""

    size: int

    async def read(self, offset: int, length: int) -> bytes: ...


class BytesSource:
    def __init__(self, data: bytes) -> None:
        self._data, self.size = data, len(data)

    async def read(self, offset: int, length: int) -> bytes:
        return self._data[offset : offset + length]


async def _read_exact(src: Source, offset: int, length: int, what: str) -> bytes:
    if offset < 0 or length < 0 or offset + length > src.size:
        raise _fail(
            ArchiveErrorCode.OUT_OF_BOUNDS, f"{what} at {offset}+{length} beyond {src.size}"
        )
    data = await src.read(offset, length)
    if len(data) != length:
        raise _fail(ArchiveErrorCode.TRUNCATED, f"{what}: short read")
    return data


@dataclass(frozen=True)
class Directory:
    cd_offset: int
    cd_size: int
    entries: int
    zip64: bool


class NameEncoding(StrEnum):
    """How an entry name was decoded. Anything but ``ascii``/``utf-8`` is reported by callers: the
    decoded name may not be what the archiver meant, so it is never used silently."""

    ASCII = "ascii"
    UTF8 = "utf-8"  # general-purpose flag bit 11 set
    UTF8_EXTRA = "utf-8-extra"  # Info-ZIP Unicode Path extra field (0x7075), CRC-checked
    UTF8_UNFLAGGED = "utf-8-unflagged"  # no flag, but the bytes are valid UTF-8 (e.g. macOS)
    CP437 = "cp437"  # no flag and not UTF-8: the ZIP default code page


@dataclass(frozen=True)
class Entry:
    index: int  # position in the central directory
    name: str
    is_dir: bool
    method: int
    flags: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_header_offset: int
    raw_name: bytes = b""  # the exact name bytes: what the local header must repeat
    name_encoding: NameEncoding = NameEncoding.ASCII


def decode_name(raw: bytes, flags: int, extra: bytes, index: int) -> tuple[str, NameEncoding]:
    """The entry name and how it was decoded. Never guesses silently: the encoding is returned."""
    if flags & 0x800:
        try:
            return raw.decode("utf-8"), NameEncoding.UTF8
        except UnicodeDecodeError as exc:
            raise _fail(ArchiveErrorCode.BAD_NAME, f"entry {index}: name is not UTF-8") from exc
    # before the ASCII shortcut: some archivers write a lossy ASCII header ("caf?") plus the real name
    unicode_path = _unicode_path_extra(raw, extra)
    if unicode_path is not None:
        return unicode_path, NameEncoding.UTF8_EXTRA
    if raw.isascii():
        return raw.decode("ascii"), NameEncoding.ASCII
    try:
        return raw.decode("utf-8"), NameEncoding.UTF8_UNFLAGGED
    except UnicodeDecodeError:
        return raw.decode("cp437"), NameEncoding.CP437


def _unicode_path_extra(raw: bytes, extra: bytes) -> str | None:
    """Info-ZIP Unicode Path (0x7075): version 1, CRC-32 of the header name, UTF-8 name. Ignored unless
    the CRC matches the name actually in the header (the field may be stale after a rename)."""
    pos = 0
    while pos + 4 <= len(extra):
        tag, length = struct.unpack_from("<HH", extra, pos)
        body = extra[pos + 4 : pos + 4 + length]
        if tag == 0x7075 and len(body) == length and length > 5 and body[0] == 1:
            if struct.unpack_from("<I", body, 1)[0] == zlib.crc32(raw):
                try:
                    return body[5:].decode("utf-8")
                except UnicodeDecodeError:
                    return None
            return None
        pos += 4 + length
    return None


def fold_name(name: str) -> str:
    """The duplicate key: Unicode NFC + case-folded (``a.json`` and ``A.json`` collide)."""
    return unicodedata.normalize("NFC", name).casefold()


def check_name(name: str, limits: ArchiveLimits) -> bool:
    """Validate an entry name; returns whether it is a directory entry. Raises BAD_NAME."""
    raw = name.encode("utf-8", "surrogatepass")
    if not name or len(raw) > limits.max_name_bytes:
        raise _fail(ArchiveErrorCode.BAD_NAME, f"name length {len(raw)}")
    if "\x00" in name or "\\" in name or name.startswith("/") or _DRIVE.match(name):
        raise _fail(ArchiveErrorCode.BAD_NAME, repr(name))
    is_dir = name.endswith("/")
    segments = name[:-1].split("/") if is_dir else name.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise _fail(ArchiveErrorCode.BAD_NAME, repr(name))
    if any(ord(c) < 0x20 for c in name):
        raise _fail(ArchiveErrorCode.BAD_NAME, repr(name))
    return is_dir


# ------------------------------------------------------------------ end of central directory
async def locate_directory(src: Source, limits: ArchiveLimits) -> Directory:
    if src.size > limits.max_archive_bytes:
        raise _fail(ArchiveErrorCode.ENTRY_TOO_LARGE, f"archive {src.size} bytes > limit")
    if src.size < EOCD_LEN:
        raise _fail(ArchiveErrorCode.TRUNCATED, "smaller than an end-of-central-directory record")
    tail_len = min(src.size, EOCD_LEN + MAX_COMMENT)
    tail_start = src.size - tail_len
    tail = await _read_exact(src, tail_start, tail_len, "tail")
    pos = -1
    for i in range(len(tail) - EOCD_LEN, -1, -1):  # last EOCD whose comment ends exactly at EOF
        if tail[i : i + 4] == b"PK\x05\x06":
            comment_len = struct.unpack_from("<H", tail, i + 20)[0]
            if i + EOCD_LEN + comment_len == len(tail):
                pos = i
                break
    if pos < 0:
        raise _fail(ArchiveErrorCode.BAD_EOCD, "no end-of-central-directory record (truncated?)")
    (_, disk, cd_disk, n_disk, n_total, cd_size, cd_offset, _) = struct.unpack_from(
        "<IHHHHIIH", tail, pos
    )
    eocd_offset = tail_start + pos
    zip64 = False
    if (disk != 0 or cd_disk != 0 or n_disk != n_total) and not (
        disk == 0xFFFF or cd_disk == 0xFFFF
    ):
        raise _fail(ArchiveErrorCode.MULTIDISK, "multi-disk archives are not supported")
    needs64 = 0xFFFF in (n_disk, n_total) or 0xFFFFFFFF in (cd_size, cd_offset) or disk == 0xFFFF
    if eocd_offset >= ZIP64_LOCATOR_LEN:
        loc = await _read_exact(
            src, eocd_offset - ZIP64_LOCATOR_LEN, ZIP64_LOCATOR_LEN, "zip64 locator"
        )
        if struct.unpack_from("<I", loc)[0] == ZIP64_LOCATOR_SIG:
            _, loc_disk, rec_offset, total_disks = struct.unpack_from("<IIQI", loc)
            if loc_disk != 0 or total_disks != 1:
                raise _fail(ArchiveErrorCode.MULTIDISK, "zip64 multi-disk")
            if rec_offset + ZIP64_EOCD_MIN > eocd_offset - ZIP64_LOCATOR_LEN:
                raise _fail(ArchiveErrorCode.BAD_ZIP64, "zip64 record outside the archive tail")
            rec = await _read_exact(src, rec_offset, ZIP64_EOCD_MIN, "zip64 record")
            (sig, _rec_size, _vm, _vn, d64, cdd64, n_disk64, n_total64, cd_size64, cd_offset64) = (
                struct.unpack_from("<IQHHIIQQQQ", rec)
            )
            if sig != ZIP64_EOCD_SIG:
                raise _fail(ArchiveErrorCode.BAD_ZIP64, "bad zip64 end record signature")
            if d64 != 0 or cdd64 != 0 or n_disk64 != n_total64:
                raise _fail(ArchiveErrorCode.MULTIDISK, "zip64 multi-disk")
            # 32-bit fields that are not "see zip64" must agree with the zip64 values
            for small, big, label in (
                (n_total, n_total64, "entries"),
                (cd_size, cd_size64, "cd size"),
                (cd_offset, cd_offset64, "cd offset"),
            ):
                if small not in (0xFFFF, 0xFFFFFFFF) and small != big:
                    raise _fail(ArchiveErrorCode.BAD_ZIP64, f"{label} disagrees with zip64 record")
            n_total, cd_size, cd_offset = n_total64, cd_size64, cd_offset64
            zip64 = True
            directory_end = rec_offset
        elif needs64:
            raise _fail(ArchiveErrorCode.BAD_ZIP64, "zip64 values without a zip64 locator")
        else:
            directory_end = eocd_offset
    elif needs64:
        raise _fail(ArchiveErrorCode.BAD_ZIP64, "zip64 values without a zip64 locator")
    else:
        directory_end = eocd_offset
    if n_total > limits.max_entries:
        raise _fail(ArchiveErrorCode.TOO_MANY_ENTRIES, f"{n_total} entries > {limits.max_entries}")
    if cd_offset + cd_size != directory_end:
        raise _fail(
            ArchiveErrorCode.BAD_CENTRAL_DIRECTORY,
            f"central directory {cd_offset}+{cd_size} does not end at {directory_end}",
        )
    if n_total and cd_size < n_total * CD_LEN:
        raise _fail(ArchiveErrorCode.BAD_CENTRAL_DIRECTORY, "directory too small for its entries")
    return Directory(cd_offset, cd_size, n_total, zip64)


# ------------------------------------------------------------------ central directory (streaming)
def _zip64_extra(
    extra: bytes, usize: int, csize: int, offset: int, index: int
) -> tuple[int, int, int]:
    pos = 0
    while pos + 4 <= len(extra):
        tag, length = struct.unpack_from("<HH", extra, pos)
        body = extra[pos + 4 : pos + 4 + length]
        if len(body) != length:
            raise _fail(
                ArchiveErrorCode.BAD_CENTRAL_DIRECTORY, f"entry {index}: extra field overruns"
            )
        if tag == 0x0001:
            vals = list(struct.unpack_from("<" + "Q" * (length // 8), body)) if length >= 8 else []
            out = []
            for current in (usize, csize, offset):
                if current == 0xFFFFFFFF:
                    if not vals:
                        raise _fail(
                            ArchiveErrorCode.BAD_ZIP64, f"entry {index}: zip64 extra too short"
                        )
                    out.append(vals.pop(0))
                else:
                    out.append(current)
            return out[0], out[1], out[2]
        pos += 4 + length
    if 0xFFFFFFFF in (usize, csize, offset):
        raise _fail(ArchiveErrorCode.BAD_ZIP64, f"entry {index}: zip64 sizes without zip64 extra")
    return usize, csize, offset


async def iter_central_directory(
    src: Source, limits: ArchiveLimits, directory: Directory | None = None
) -> AsyncIterator[Entry]:
    """Yield every entry, validated, reading the directory in bounded chunks (memory independent of
    the number of entries). Enforces entry count, per-entry and total size and ratio limits."""
    d = directory or await locate_directory(src, limits)
    total_cap = limits.total_cap(src.size)
    total = 0
    buf = b""
    at = 0  # parse position in buf (no per-record copying: bytes slicing is O(len))
    read_pos = d.cd_offset
    end = d.cd_offset + d.cd_size

    async def fill(need: int) -> None:
        nonlocal buf, at, read_pos
        if len(buf) - at >= need:
            return
        buf = buf[at:]
        at = 0
        while len(buf) < need and read_pos < end:
            n = min(limits.read_chunk, end - read_pos)
            buf += await _read_exact(src, read_pos, n, "central directory")
            read_pos += n

    index = 0
    while index < d.entries:
        await fill(CD_LEN)
        if len(buf) - at < CD_LEN:
            raise _fail(
                ArchiveErrorCode.BAD_CENTRAL_DIRECTORY, f"directory ends inside entry {index}"
            )
        (sig, made_by, _need, flags, method, _t, _d, crc, csize, usize, nlen, xlen, clen, disk, _ia, ext_attr, offset) = (
            struct.unpack_from("<IHHHHHHIIIHHHHHII", buf, at)
        )  # fmt: skip
        if sig != CD_SIG:
            raise _fail(ArchiveErrorCode.BAD_CENTRAL_DIRECTORY, f"entry {index}: bad signature")
        rec_len = CD_LEN + nlen + xlen + clen
        await fill(rec_len)
        if len(buf) - at < rec_len:
            raise _fail(
                ArchiveErrorCode.BAD_CENTRAL_DIRECTORY, f"directory ends inside entry {index}"
            )
        raw_name = buf[at + CD_LEN : at + CD_LEN + nlen]
        extra = buf[at + CD_LEN + nlen : at + CD_LEN + nlen + xlen]
        at += rec_len
        if disk not in (0, 0xFFFF):
            raise _fail(ArchiveErrorCode.MULTIDISK, f"entry {index} on disk {disk}")
        if flags & 0x1 or method == 99:
            raise _fail(ArchiveErrorCode.ENCRYPTED, f"entry {index} is encrypted")
        if method not in (STORED, DEFLATED):
            raise _fail(ArchiveErrorCode.METHOD, f"entry {index}: method {method}")
        name, encoding = decode_name(raw_name, flags, extra, index)
        is_dir = check_name(name, limits)
        if made_by >> 8 == 3 and (ext_attr >> 16) & 0o170000 == 0o120000:
            raise _fail(ArchiveErrorCode.SYMLINK, f"entry {index} {name!r} is a symlink")
        usize, csize, offset = _zip64_extra(extra, usize, csize, offset, index)
        if offset + LOCAL_LEN + nlen > d.cd_offset:
            raise _fail(
                ArchiveErrorCode.OUT_OF_BOUNDS, f"entry {index}: header outside the data area"
            )
        if usize > limits.max_entry_bytes:
            raise _fail(ArchiveErrorCode.ENTRY_TOO_LARGE, f"entry {index}: {usize} bytes")
        if method == STORED and usize != csize:
            raise _fail(ArchiveErrorCode.SIZE_MISMATCH, f"entry {index}: stored sizes differ")
        if usize > limits.ratio_floor_bytes and usize > limits.max_entry_ratio * max(csize, 1):
            raise _fail(ArchiveErrorCode.RATIO, f"entry {index}: ratio {usize}/{csize}")
        if offset + csize > d.cd_offset:
            raise _fail(ArchiveErrorCode.OUT_OF_BOUNDS, f"entry {index}: data beyond the data area")
        total += usize
        if total > total_cap:
            raise _fail(ArchiveErrorCode.TOTAL_TOO_LARGE, f"decompressed total exceeds {total_cap}")
        if is_dir and usize:
            raise _fail(ArchiveErrorCode.BAD_NAME, f"directory entry {name!r} has data")
        yield Entry(
            index, name, is_dir, method, flags, crc, csize, usize, offset, raw_name, encoding
        )
        index += 1
    if len(buf) - at or read_pos != end:
        raise _fail(
            ArchiveErrorCode.BAD_CENTRAL_DIRECTORY, "trailing bytes in the central directory"
        )


# ------------------------------------------------------------------ entry data
@dataclass
class EntryDigest:
    sha256: str
    size: int
    crc32: int


async def data_offset(src: Source, entry: Entry, limits: ArchiveLimits) -> int:
    """Validate the local header against the central directory; return where the data starts."""
    head = await _read_exact(src, entry.local_header_offset, LOCAL_LEN, "local header")
    (sig, _ver, flags, method, _t, _d, crc, csize, usize, nlen, xlen) = struct.unpack_from(
        "<IHHHHHIIIHH", head
    )
    if sig != LOCAL_SIG:
        raise _fail(ArchiveErrorCode.HEADER_MISMATCH, f"entry {entry.index}: bad local signature")
    name = await _read_exact(src, entry.local_header_offset + LOCAL_LEN, nlen, "local name")
    expected = entry.raw_name or entry.name.encode("utf-8" if entry.flags & 0x800 else "cp437")
    if name != expected or method != entry.method or (flags & 0x1):
        raise _fail(ArchiveErrorCode.HEADER_MISMATCH, f"entry {entry.index}: local header differs")
    if not flags & 0x8 and crc != entry.crc32:  # without a data descriptor the local CRC must match
        raise _fail(ArchiveErrorCode.HEADER_MISMATCH, f"entry {entry.index}: local CRC differs")
    if (
        not flags & 0x8
        and 0xFFFFFFFF not in (csize, usize)
        and (csize, usize) != (entry.compressed_size, entry.uncompressed_size)
    ):
        raise _fail(ArchiveErrorCode.HEADER_MISMATCH, f"entry {entry.index}: local sizes differ")
    start = entry.local_header_offset + LOCAL_LEN + nlen + xlen
    if start + entry.compressed_size > src.size:
        raise _fail(ArchiveErrorCode.OUT_OF_BOUNDS, f"entry {entry.index}: data beyond the archive")
    _ = limits
    return int(start)


async def open_entry(
    src: Source,
    entry: Entry,
    limits: ArchiveLimits,
    *,
    data_end_limit: int | None = None,
    on_digest: Callable[[EntryDigest], None] | None = None,
) -> AsyncIterator[bytes]:
    """Stream the decompressed bytes of ``entry``. Checks the local header, that the data stays below
    ``data_end_limit`` (the next entry's header in local-header order: overlap detection), the declared
    size (never one byte more is produced) and the CRC-32. Calls ``on_digest`` with SHA-256/size/CRC at
    the end; any mismatch raises ``ArchiveError`` before ``on_digest`` runs."""
    start = await data_offset(src, entry, limits)
    end = start + entry.compressed_size
    if data_end_limit is not None and end > data_end_limit:
        raise _fail(ArchiveErrorCode.OVERLAP, f"entry {entry.index} overlaps the next entry")
    sha = hashlib.sha256()
    state = {"crc": 0, "produced": 0}
    limit = entry.uncompressed_size
    inflater = zlib.decompressobj(-15) if entry.method == DEFLATED else None

    def take(piece: bytes) -> bytes:
        state["produced"] += len(piece)
        if state["produced"] > limit:  # never one byte past the declared size (bomb guard)
            raise _fail(ArchiveErrorCode.SIZE_MISMATCH, f"entry {entry.index}: more than declared")
        sha.update(piece)
        state["crc"] = zlib.crc32(piece, state["crc"])
        return piece

    pos = start
    while pos < end:
        n = min(limits.read_chunk, end - pos)
        data = await _read_exact(src, pos, n, f"entry {entry.index} data")
        pos += n
        # one chunk of inflating and hashing at a time: a source whose reads complete without
        # suspending (coalesced, in memory) must not keep the event loop for a whole entry. The
        # synchronous drivers (`package_source._run`, `rsmf_check._run`) step over this turn.
        await asyncio.sleep(0)
        if inflater is None:
            yield take(data)
            continue
        while data:
            try:
                out = inflater.decompress(data, limits.read_chunk)  # output bounded per call
            except zlib.error as exc:
                raise _fail(ArchiveErrorCode.CORRUPT_DATA, f"entry {entry.index}: {exc}") from exc
            data = inflater.unconsumed_tail
            if out:
                yield take(out)
            if inflater.eof:
                break
        if inflater.eof and (inflater.unused_data or pos < end):
            raise _fail(
                ArchiveErrorCode.CORRUPT_DATA, f"entry {entry.index}: data after the stream end"
            )
    if inflater is not None and not inflater.eof:
        try:
            tail = inflater.flush()
        except zlib.error as exc:
            raise _fail(ArchiveErrorCode.CORRUPT_DATA, f"entry {entry.index}: {exc}") from exc
        if tail:
            yield take(tail)
        if not inflater.eof:
            raise _fail(
                ArchiveErrorCode.CORRUPT_DATA, f"entry {entry.index}: deflate stream incomplete"
            )
    produced, crc = state["produced"], state["crc"]
    if produced != limit:
        raise _fail(ArchiveErrorCode.SIZE_MISMATCH, f"entry {entry.index}: {produced} != {limit}")
    if crc != entry.crc32:
        raise _fail(ArchiveErrorCode.CRC_MISMATCH, f"entry {entry.index}: CRC-32 mismatch")
    if on_digest is not None:
        on_digest(EntryDigest(sha.hexdigest(), produced, crc))


async def read_entry(src: Source, entry: Entry, limits: ArchiveLimits) -> tuple[bytes, EntryDigest]:
    """Whole entry in memory (bounded by ``max_entry_bytes``): for day files, which are small."""
    digest: list[EntryDigest] = []
    parts = [c async for c in open_entry(src, entry, limits, on_digest=digest.append)]
    return b"".join(parts), digest[0]


async def scan(src: Source, limits: ArchiveLimits) -> list[Entry]:
    """Validate a SMALL archive completely (directory, duplicates, every entry's data in local-header
    order). For tests, fuzzing and the verifier's small packages; the worker streams instead."""
    entries = [e async for e in iter_central_directory(src, limits)]
    seen: set[str] = set()
    for e in entries:
        key = fold_name(e.name)
        if key in seen:
            raise _fail(ArchiveErrorCode.DUPLICATE_NAME, repr(e.name))
        seen.add(key)
    directory = await locate_directory(src, limits)
    ordered = sorted(entries, key=lambda e: e.local_header_offset)
    for i, e in enumerate(ordered):
        nxt = ordered[i + 1].local_header_offset if i + 1 < len(ordered) else directory.cd_offset
        async for _ in open_entry(src, e, limits, data_end_limit=nxt):
            pass
    return entries
