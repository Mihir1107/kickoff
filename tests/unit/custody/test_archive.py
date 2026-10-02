"""The hardened ZIP reader (ADR 0014 section 3): valid archives, every rejection rule, bounded memory."""

from __future__ import annotations

import asyncio
import hashlib
import struct
import time
import zipfile
import zlib

import pytest

from edisc_custody.archive import (
    ArchiveError,
    ArchiveErrorCode,
    ArchiveLimits,
    BytesSource,
    locate_directory,
    read_entry,
    scan,
)

from .zips import cd_offset, make_zip, patch_cd, zip64_central_directory

C = ArchiveErrorCode
SMALL = ArchiveLimits(
    max_entry_bytes=1 << 20, max_total_bytes=8 << 20, ratio_floor_bytes=4096, read_chunk=4096
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _scan(data: bytes, limits: ArchiveLimits = SMALL) -> list[str]:
    return [e.name for e in await scan(BytesSource(data), limits)]


def code_of(data: bytes, limits: ArchiveLimits = SMALL) -> ArchiveErrorCode:
    with pytest.raises(ArchiveError) as exc:
        run(_scan(data, limits))
    return exc.value.code


# ------------------------------------------------------------------ valid archives
@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
@pytest.mark.parametrize("zip64", [False, True])
def test_valid_archive_reads_with_hashes(method: int, zip64: bool) -> None:
    files = {
        "users.json": b'[{"id": "U1"}]',
        "general/": b"",
        "general/2026-01-05.json": b'[{"ts": "1767571200.000100", "text": "hi"}]' * 50,
        "random/2026-01-06.json": bytes(range(256)) * 40,
    }
    data = make_zip(files, method=method, zip64=zip64)
    src = BytesSource(data)
    entries = run(scan(src, SMALL))
    assert [e.name for e in entries] == list(files)
    for e in entries:
        body, digest = run(read_entry(src, e, SMALL))
        assert body == files[e.name]
        assert digest.sha256 == hashlib.sha256(files[e.name]).hexdigest()
        assert digest.crc32 == zlib.crc32(files[e.name]) and digest.size == len(files[e.name])


PROBE = """
import asyncio, resource, sys
sys.path.insert(0, {here!r})
from zips import SyntheticArchive
from edisc_custody.archive import ArchiveLimits, iter_central_directory

def rss() -> int:
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v if sys.platform == "darwin" else v * 1024

async def main() -> None:
    archive = SyntheticArchive({n})
    base = rss()
    count, last = 0, None
    async for e in iter_central_directory(archive, ArchiveLimits(read_chunk=1 << 20)):
        count, last = count + 1, e
    print(count, last.name, last.local_header_offset, base, rss(), archive.reads, archive.cd_size)

asyncio.run(main())
"""


def test_zip64_archive_with_millions_of_entries_streams_in_bounded_memory() -> None:
    """R1: the central directory (3M entries, 252 MB) is parsed as a stream in a fresh process; peak
    memory growth stays small and independent of the entry count (tracemalloc is too slow at this size,
    so the process's max RSS is measured instead)."""
    import subprocess
    import sys
    from pathlib import Path

    n = 3_000_000
    code = PROBE.format(here=str(Path(__file__).parent), n=n)
    started = time.monotonic()
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=110
    )
    count, last, last_offset, base, peak, reads, cd_size = out.stdout.split()
    assert int(count) == n and last == f"d{n - 1:08d}.json"
    assert int(last_offset) == (n - 1) * (30 + 14)
    assert int(peak) - int(base) < 48 * 1024 * 1024, f"grew {(int(peak) - int(base)) / 1e6:.1f} MB"
    assert int(reads) <= int(cd_size) // (1 << 20) + 5  # bounded 1 MiB reads, not per entry
    assert time.monotonic() - started < 110


# ------------------------------------------------------------------ rejections, one per rule
def test_truncated_archives_are_rejected() -> None:
    data = make_zip({"a.json": b"x" * 5000})
    for cut in (1, 10, 21, len(data) // 2, len(data) - 1):
        assert code_of(data[: len(data) - cut]) in {
            C.BAD_EOCD,
            C.BAD_CENTRAL_DIRECTORY,
            C.TRUNCATED,
            C.OUT_OF_BOUNDS,
        }


@pytest.mark.parametrize(
    "name",
    [
        "../escape.json",
        "a/../../b.json",
        "/abs.json",
        "C:/win.json",
        "a\\b.json",
        "a//b.json",
        "./a.json",
        "bad\x01.json",
    ],
)
def test_dangerous_names_are_rejected(name: str) -> None:
    assert code_of(make_zip({name: b"[]"})) is C.BAD_NAME


def test_overlong_names_are_rejected() -> None:
    limits = ArchiveLimits(max_name_bytes=32, read_chunk=4096)
    assert code_of(make_zip({"x" * 40: b"[]"}), limits) is C.BAD_NAME


def test_duplicates_including_case_and_unicode_folding_are_rejected() -> None:
    for a, b in (("a.json", "A.json"), ("caf\u00e9.json", "cafe\u0301.json")):
        assert code_of(make_zip({a: b"1", b: b"2"})) is C.DUPLICATE_NAME
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("same.json", b"1")
        with pytest.warns(UserWarning, match="Duplicate name"):
            zf.writestr("same.json", b"2")
    assert code_of(buf.getvalue()) is C.DUPLICATE_NAME


def test_symlinks_are_rejected() -> None:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link.json")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
    assert code_of(buf.getvalue()) is C.SYMLINK


def test_encrypted_and_unsupported_methods_are_rejected() -> None:
    data = make_zip({"a.json": b"[]"})
    encrypted = patch_cd(data, 0, "<H", 8, 0x1)
    assert code_of(encrypted) is C.ENCRYPTED
    assert code_of(make_zip({"a.json": b"[]" * 100}, method=zipfile.ZIP_BZIP2)) is C.METHOD


def test_size_ratio_and_total_limits() -> None:
    bomb = make_zip({"bomb.json": b"\0" * (1 << 20)})  # ~1000:1
    assert (
        code_of(bomb, ArchiveLimits(ratio_floor_bytes=4096, max_entry_ratio=200, read_chunk=4096))
        is C.RATIO
    )
    assert (
        code_of(
            make_zip({"big.json": b"x" * 5000}),
            ArchiveLimits(max_entry_bytes=4000, read_chunk=4096),
        )
        is C.ENTRY_TOO_LARGE
    )
    many = make_zip({f"f{i}.json": b"y" * 1000 for i in range(10)})
    limits = ArchiveLimits(max_total_bytes=5000, read_chunk=4096, ratio_floor_bytes=1 << 30)
    assert code_of(many, limits) is C.TOTAL_TOO_LARGE
    assert code_of(many, ArchiveLimits(max_entries=5, read_chunk=4096)) is C.TOO_MANY_ENTRIES


def test_lying_sizes_never_produce_more_than_declared() -> None:
    """A bomb that declares a small size is stopped at the declared size, not at its real size."""
    data = make_zip({"a.json": b"\0" * 200_000})
    lying = patch_cd(data, 0, "<I", 24, 1000)  # uncompressed size field
    assert code_of(lying, ArchiveLimits(read_chunk=4096, ratio_floor_bytes=1 << 30)) in {
        C.SIZE_MISMATCH,
        C.HEADER_MISMATCH,
    }


def test_crc_and_corrupt_data_are_detected() -> None:
    stored = make_zip({"a.json": b"hello world" * 10}, method=zipfile.ZIP_STORED)
    pos = stored.index(b"hello world")
    flipped = stored[:pos] + b"j" + stored[pos + 1 :]
    assert code_of(flipped) is C.CRC_MISMATCH
    deflated = make_zip({"a.json": bytes(range(256)) * 100})
    start = deflated.index(b"a.json") + len(b"a.json")
    broken = (
        deflated[:start]
        + bytes(b ^ 0xFF for b in deflated[start : start + 40])
        + deflated[start + 40 :]
    )
    assert code_of(broken) in {C.CORRUPT_DATA, C.CRC_MISMATCH, C.SIZE_MISMATCH}


def test_overlapping_entries_are_rejected() -> None:
    """Two directory records pointing at the same data (the classic overlapping bomb)."""
    data = make_zip({"a.json": b"[1]", "b.json": b"[2]"})
    first_header = struct.unpack_from("<I", data, cd_offset(data) + 42)[0]
    overlapping = patch_cd(data, 1, "<I", 42, first_header)
    assert code_of(overlapping) in {C.HEADER_MISMATCH, C.OVERLAP}


def test_directory_disagreeing_with_the_end_record_is_rejected() -> None:
    data = make_zip({"a.json": b"[1]"})
    pos = data.rfind(b"PK\x05\x06")
    bad = bytearray(data)
    struct.pack_into("<I", bad, pos + 16, cd_offset(data) - 1)
    assert code_of(bytes(bad)) in {C.BAD_CENTRAL_DIRECTORY, C.OUT_OF_BOUNDS}
    with pytest.raises(ArchiveError):
        run(locate_directory(BytesSource(b"PK\x05\x06" + b"\0" * 10), SMALL))


def test_zip64_extra_fields_in_the_central_directory_are_honoured_and_checked() -> None:
    files = {"a.json": b"[1]" * 100, "b/2026-01-05.json": b"[2]"}
    plain = make_zip(files)
    data = zip64_central_directory(plain)
    assert run(_scan(data)) == list(files)
    # an extra field that claims more bytes than the record has
    first = cd_offset(plain)  # unchanged; the end record now says "see zip64"
    nlen = struct.unpack_from("<H", data, first + 28)[0]
    short = bytearray(data)
    struct.pack_into("<H", short, first + 46 + nlen + 2, 64)  # zip64 extra length 24 -> 64
    assert code_of(bytes(short)) in {C.BAD_CENTRAL_DIRECTORY, C.BAD_ZIP64}


def test_name_decoding_is_reported_never_guessed_silently() -> None:
    import struct as st

    from edisc_custody.archive import NameEncoding, decode_name

    assert decode_name(b"a.json", 0, b"", 0) == ("a.json", NameEncoding.ASCII)
    assert decode_name("café".encode(), 0x800, b"", 0) == ("café", NameEncoding.UTF8)
    assert decode_name("café".encode(), 0, b"", 0) == ("café", NameEncoding.UTF8_UNFLAGGED)
    assert decode_name("café".encode("cp437"), 0, b"", 0) == ("café", NameEncoding.CP437)
    header = b"caf?"
    body = st.pack("<BI", 1, zlib.crc32(header)) + "café".encode()
    extra = st.pack("<HH", 0x7075, len(body)) + body
    assert decode_name(header, 0, extra, 0) == ("café", NameEncoding.UTF8_EXTRA)
    stale = st.pack("<HH", 0x7075, len(body)) + st.pack("<BI", 1, 0) + "café".encode()
    assert decode_name(header, 0, stale, 0) == ("caf?", NameEncoding.ASCII)  # stale CRC: ignored
    with pytest.raises(ArchiveError):
        decode_name(b"\xff", 0x800, b"", 0)  # flagged UTF-8 that is not UTF-8
