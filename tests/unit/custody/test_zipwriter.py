"""The deterministic package zip writer (ADR 0015 §19): STORED, fixed metadata, a data descriptor with
the streamed CRC-32 on EVERY entry, ZIP64 only where needed, and archives that our hardened reader,
Python's ``zipfile``, Info-ZIP ``unzip``, 7-Zip and macOS ``ditto`` all open. ZIP64 is exercised past
65,535 entries (on disk) and past 4 GiB (a synthetic stream into a hashing sink, read back through a
seekable synthetic source: no 4 GiB file is ever written)."""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from edisc_custody.archive import (
    ArchiveError,
    iter_central_directory,
    locate_directory,
    open_entry,
    scan,
)
from edisc_custody.package_source import PACKAGE_LIMITS, ZipSource, read_all
from edisc_custody.zipwriter import MAX32, ZipMember, ZipSizeError, ZipSizer, zip_stream


def member(name: str, data: bytes, *, declared: int | None = None, pieces: int = 3) -> ZipMember:
    async def chunks() -> AsyncIterator[bytes]:
        step = max(1, len(data) // pieces)
        for i in range(0, len(data), step):
            yield data[i : i + step]

    return ZipMember(name, len(data) if declared is None else declared, chunks)


async def members(items: list[ZipMember]) -> AsyncIterator[ZipMember]:
    for m in items:
        yield m


async def build(items: list[ZipMember]) -> bytes:
    return b"".join([c async for c in zip_stream(members(items))])


def sized(entries: list[tuple[str, int]]) -> int:
    sizer = ZipSizer()
    for name, size in entries:
        sizer.add(name, size)
    return sizer.total()


SAMPLE = {
    "manifest.json": b'{"format":"x"}',
    "events.jsonl": b"line one\nline two\n",
    "empty.txt": b"",
    "objects/" + "ab" * 32: bytes(range(256)) * 300,
    "outputs/café 日本.rsmf": b"MIME-Version: 1.0\r\n\r\nbody",
}


def tool(*names: str) -> str:
    """A command-line unzipper. Missing locally: skipped; missing in CI: a failure."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if os.environ.get("CI"):
        pytest.fail(f"none of {names} installed on the CI runner")
    pytest.skip(f"none of {names} installed")


# ------------------------------------------------------------------ format
async def test_round_trip_and_fixed_metadata() -> None:
    data = await build([member(n, d) for n, d in SAMPLE.items()])
    assert sized([(n, len(d)) for n, d in SAMPLE.items()]) == len(data)  # the Content-Length
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None  # every CRC checked
        assert zf.namelist() == list(SAMPLE)  # the order given
        for info in zf.infolist():
            assert zf.read(info) == SAMPLE[info.filename]
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr >> 16 == 0o100644
            assert info.flag_bits == 0x0808  # UTF-8 names + data descriptor, on every entry
            assert info.extra == b""  # no extra fields when ZIP64 is not needed
            assert info.comment == b""
    entries = await scan(_Bytes(data), PACKAGE_LIMITS)  # our hardened reader accepts it
    assert [e.name for e in entries] == list(SAMPLE)
    assert data.count(b"PK\x07\x08") == len(SAMPLE)  # one descriptor per entry
    for (
        e
    ) in entries:  # the local header carries no CRC or sizes; the descriptor after the data does
        _sig, _v, flags, _m, _t, _d, crc, csize, usize, nlen, xlen = struct.unpack_from(
            "<IHHHHHIIIHH", data, e.local_header_offset
        )
        assert (flags & 0x8, crc, csize, usize, xlen) == (0x8, 0, 0, 0, 0)
        end = e.local_header_offset + 30 + nlen + e.uncompressed_size
        assert struct.unpack_from("<IIII", data, end) == (
            0x08074B50, e.crc32, e.uncompressed_size, e.uncompressed_size,
        )  # fmt: skip
        assert e.crc32 == zlib.crc32(SAMPLE[e.name])


async def test_the_same_members_give_the_same_bytes_however_they_are_chunked() -> None:
    a = await build([member(n, d, pieces=1) for n, d in SAMPLE.items()])
    b = await build([member(n, d, pieces=7) for n, d in SAMPLE.items()])
    assert a == b


async def test_a_member_that_differs_from_its_declared_size_raises() -> None:
    with pytest.raises(ZipSizeError, match="more than the declared"):
        await build([member("a", b"12345", declared=4)])
    with pytest.raises(ZipSizeError, match="declared 6"):
        await build([member("a", b"12345", declared=6)])
    with pytest.raises(ValueError, match="duplicate"):
        await build([member("a", b"1"), member("a", b"2")])


async def test_more_bytes_are_refused_before_they_are_passed_on() -> None:
    out: list[bytes] = []

    async def drain() -> None:
        async for chunk in zip_stream(members([member("a", b"x" * 10, declared=5, pieces=10)])):
            out.append(chunk)  # noqa: PERF401 (kept up to the failure)

    with pytest.raises(ZipSizeError):
        await drain()
    assert b"".join(out).count(b"x") == 5  # never one byte past the declared size


# ------------------------------------------------------------------ the verifier's zip reader
def test_zip_source_reads_entries_and_refuses_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "p.zip"
    path.write_bytes(asyncio.run(build([member(n, d) for n, d in SAMPLE.items()])))
    src = ZipSource(path)
    try:
        assert src.names() == sorted(SAMPLE)
        assert read_all(src, "events.jsonl") == SAMPLE["events.jsonl"]
    finally:
        src.close()
    dup = tmp_path / "dup.zip"
    dup.write_bytes(asyncio.run(build([member("A.txt", b"1"), member("a.txt", b"2")])))
    with pytest.raises(ArchiveError, match="duplicate_name"):
        ZipSource(dup)


def test_zip_source_catches_a_flipped_byte(tmp_path: Path) -> None:
    data = bytearray(asyncio.run(build([member("x.bin", b"\x00" * 1000)])))
    data[30 + len("x.bin") + 500] ^= 0xFF
    path = tmp_path / "t.zip"
    path.write_bytes(bytes(data))
    src = ZipSource(path)
    try:
        with pytest.raises(ArchiveError, match="crc_mismatch"):
            read_all(src, "x.bin")
    finally:
        src.close()


# ------------------------------------------------------------------ other unzippers
@pytest.fixture(scope="module")
def sample_zip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("zips") / "sample.zip"
    path.write_bytes(asyncio.run(build([member(n, d) for n, d in SAMPLE.items()])))
    return path


def _extracted_equal(root: Path) -> None:
    for name, data in SAMPLE.items():
        assert (root / name).read_bytes() == data, name


def test_info_zip_unzip_extracts_it(sample_zip: Path, tmp_path: Path) -> None:
    unzip = tool("unzip")
    subprocess.run([unzip, "-tq", sample_zip], check=True, capture_output=True)
    subprocess.run([unzip, "-q", sample_zip, "-d", tmp_path], check=True, capture_output=True)
    _extracted_equal(tmp_path)


def test_7zip_extracts_it(sample_zip: Path, tmp_path: Path) -> None:
    seven = tool("7zz", "7z")
    subprocess.run([seven, "t", sample_zip], check=True, capture_output=True)
    subprocess.run([seven, "x", f"-o{tmp_path}", sample_zip], check=True, capture_output=True)
    _extracted_equal(tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="ditto is macOS only")
def test_macos_ditto_extracts_it(sample_zip: Path, tmp_path: Path) -> None:
    """``ditto -x -k`` uses the same extraction as Archive Utility (checked by hand once too)."""
    subprocess.run(["ditto", "-x", "-k", sample_zip, tmp_path], check=True, capture_output=True)
    _extracted_equal(tmp_path)


# ------------------------------------------------------------------ ZIP64: more than 65,535 entries
def test_more_than_65535_entries(tmp_path: Path) -> None:
    n = 70_000
    names = [f"objects/{i:06d}" for i in range(n)]
    data = asyncio.run(build([member(name, name.encode()[-6:], pieces=1) for name in names]))
    assert sized([(name, 6) for name in names]) == len(data)
    eocd = data.rfind(b"PK\x05\x06")
    assert struct.unpack_from("<HH", data, eocd + 8) == (0xFFFF, 0xFFFF)
    assert data.rfind(b"PK\x06\x06") > 0 and data.rfind(b"PK\x06\x07") > 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert len(zf.infolist()) == n
        assert zf.read(names[-1]) == names[-1].encode()[-6:]
        assert all(
            i.extra == b"" for i in zf.infolist()
        )  # entries and offsets fit: no ZIP64 extras
    src = _Bytes(data)
    directory = asyncio.run(locate_directory(src, PACKAGE_LIMITS))
    assert directory.zip64 and directory.entries == n
    path = tmp_path / "many.zip"
    path.write_bytes(data)
    zsrc = ZipSource(path)
    try:
        assert len(zsrc.names()) == n
    finally:
        zsrc.close()
    unzip = shutil.which("unzip")
    if unzip:
        subprocess.run([unzip, "-tq", path], check=True, capture_output=True)
    seven = shutil.which("7zz") or shutil.which("7z")
    if seven:
        subprocess.run([seven, "t", path], check=True, capture_output=True)


# ------------------------------------------------------------------ ZIP64: more than 4 GiB
PATTERN = bytes(range(256)) * 4096  # 1 MiB, the same object every time
BIG = (4 << 30) + (1 << 20)  # 4 GiB + 1 MiB: above 0xFFFFFFFF


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data, self.size = data, len(data)

    async def read(self, offset: int, length: int) -> bytes:
        return self._data[offset : offset + length]


class SyntheticArchive:
    """The archive as recorded from the stream: literal bytes for headers and the directory, and a
    reference for every chunk that IS the pattern. Random access without the bytes on disk."""

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.parts: list[bytes | None] = []  # None = PATTERN
        self.size = 0
        self.sha256 = hashlib.sha256()

    def add(self, chunk: bytes) -> None:
        self.starts.append(self.size)
        self.parts.append(None if chunk is PATTERN else bytes(chunk))
        self.size += len(chunk)
        self.sha256.update(chunk)

    def read_at(self, offset: int, length: int) -> bytes:
        out = bytearray()
        i = bisect.bisect_right(self.starts, offset) - 1
        while length > 0 and i < len(self.parts):
            part = self.parts[i] if self.parts[i] is not None else PATTERN
            assert part is not None
            lo = offset - self.starts[i]
            piece = part[lo : lo + length]
            out += piece
            offset += len(piece)
            length -= len(piece)
            i += 1
        return bytes(out)

    async def read(self, offset: int, length: int) -> bytes:  # edisc_custody.archive.Source
        return self.read_at(offset, length)


class SyntheticFile(io.RawIOBase):
    """A seekable file object over the synthetic archive, for ``zipfile``."""

    def __init__(self, archive: SyntheticArchive) -> None:
        self.archive, self.pos = archive, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = [offset, self.pos + offset, self.archive.size + offset][whence]
        return self.pos

    def read(self, n: int | None = -1) -> bytes:
        if n is None or n < 0:
            n = self.archive.size - self.pos
        data = self.archive.read_at(self.pos, n)
        self.pos += len(data)
        return data


async def test_more_than_4_gib_through_a_hashing_sink() -> None:
    entry_sha = hashlib.sha256()

    async def big() -> AsyncIterator[bytes]:
        for _ in range(BIG // len(PATTERN)):
            entry_sha.update(PATTERN)
            yield PATTERN

    archive = SyntheticArchive()
    tail = b"after the big entry: its offset is past 4 GiB"
    async for chunk in zip_stream(
        members([ZipMember("big.bin", BIG, big), member("tail.txt", tail)])
    ):
        archive.add(chunk)
    assert archive.size > BIG > MAX32
    assert sized([("big.bin", BIG), ("tail.txt", len(tail))]) == archive.size  # ZIP64 sizes too
    want = entry_sha.hexdigest()

    # our hardened streaming reader
    directory = await locate_directory(archive, PACKAGE_LIMITS)
    assert directory.zip64 and directory.cd_offset > MAX32
    entries = [e async for e in iter_central_directory(archive, PACKAGE_LIMITS, directory)]
    assert [(e.name, e.uncompressed_size) for e in entries] == [
        ("big.bin", BIG),
        ("tail.txt", len(tail)),
    ]
    assert entries[1].local_header_offset > MAX32
    digests: list[object] = []
    async for _ in open_entry(archive, entries[0], PACKAGE_LIMITS, on_digest=digests.append):
        pass
    assert digests[0].sha256 == want  # type: ignore[attr-defined]  (CRC and size checked inside)
    small = b"".join([c async for c in open_entry(archive, entries[1], PACKAGE_LIMITS)])
    assert small == tail

    # Python's zipfile over a seekable synthetic source
    with zipfile.ZipFile(SyntheticFile(archive)) as zf:  # type: ignore[arg-type]
        info = zf.getinfo("big.bin")
        assert info.file_size == BIG and info.flag_bits == 0x0808
        h = hashlib.sha256()
        with zf.open(info) as fh:  # zipfile checks the CRC at the end of the entry
            for block in iter(lambda: fh.read(1 << 22), b""):
                h.update(block)
        assert h.hexdigest() == want
        assert zf.read("tail.txt") == tail
        assert zf.getinfo("tail.txt").header_offset > MAX32
