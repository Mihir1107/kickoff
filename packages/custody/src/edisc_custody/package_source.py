"""Read a package from a directory or straight from its zip (ADR 0015 §19). Pure: standard library and
``edisc_custody.archive`` only, for the offline verifier.

A zip is read with our hardened reader (``edisc_custody.archive``), never extracted: every entry's local
header must match the central directory, entries must not overlap, names must be safe and unique
(``fold_name``), and each entry's CRC-32 and size are checked as it is read. Its coroutines are driven
synchronously here: the file source never suspends, so this works with or without a running event loop.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Awaitable, Iterator
from pathlib import Path
from typing import Protocol, cast

from edisc_custody.archive import (
    ArchiveError,
    ArchiveErrorCode,
    ArchiveLimits,
    Entry,
    data_offset,
    fold_name,
    iter_central_directory,
    locate_directory,
    open_entry,
)

STORED = 0

CHUNK = 1 << 20

# a package is our own output, not an upload: only the structural checks matter, never the size caps
PACKAGE_LIMITS = ArchiveLimits(
    max_archive_bytes=1 << 62,
    max_entries=1 << 40,
    max_entry_bytes=1 << 62,
    max_total_bytes=1 << 62,
    max_total_ratio=1 << 20,
    max_entry_ratio=1 << 20,
)


class PackageSource(Protocol):
    label: str

    def names(self) -> list[str]: ...

    def exists(self, name: str) -> bool: ...

    def chunks(self, name: str) -> Iterator[bytes]: ...


def read_all(source: PackageSource, name: str, limit: int = 64 << 20) -> bytes:
    out = bytearray()
    for chunk in source.chunks(name):
        out += chunk
        if len(out) > limit:
            raise ValueError(f"{name}: larger than {limit} bytes")
    return bytes(out)


def lines(source: PackageSource, name: str) -> Iterator[bytes]:
    """The file's lines, each with its newline (the last one may lack it), in bounded memory."""
    rest = b""
    for chunk in source.chunks(name):
        rest += chunk
        *complete, rest = rest.split(b"\n")
        for line in complete:
            yield line + b"\n"
    if rest:
        yield rest


class DirectorySource:
    def __init__(self, root: Path) -> None:
        self.root, self.label = root, str(root)

    def names(self) -> list[str]:
        return sorted(
            Path(base, f).relative_to(self.root).as_posix()
            for base, _dirs, files in os.walk(self.root)
            for f in files
        )

    def exists(self, name: str) -> bool:
        return (self.root / name).is_file()

    def chunks(self, name: str) -> Iterator[bytes]:
        with (self.root / name).open("rb") as fh:
            yield from iter(lambda: fh.read(CHUNK), b"")


class _FileRange:
    def __init__(self, path: Path) -> None:
        self._fh = path.open("rb")
        self.size = path.stat().st_size

    async def read(self, offset: int, length: int) -> bytes:
        self._fh.seek(offset)
        return self._fh.read(length)

    def read_now(self, offset: int, length: int) -> bytes:
        self._fh.seek(offset)
        return self._fh.read(length)

    def close(self) -> None:
        self._fh.close()


class StoredRange:
    """Synchronous random access to the bytes of one STORED zip entry (``size`` from its record)."""

    def __init__(self, src: _FileRange, start: int, size: int) -> None:
        self._src, self._start, self.size = src, start, size

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self.size - offset))
        return self._src.read_now(self._start + offset, length) if length else b""


def _run[T](awaitable: Awaitable[T]) -> T:
    """Run an awaitable that never suspends (file reads only) to completion, without an event loop."""
    steps = awaitable.__await__()
    try:
        next(steps)
    except StopIteration as done:
        return cast("T", done.value)
    raise RuntimeError("archive read suspended: not a synchronous source")


class ZipSource:
    """A zip package. Duplicate names (after case and Unicode folding) and unsafe names refuse the
    whole package with ``ArchiveError``."""

    def __init__(self, path: Path) -> None:
        self.label = str(path)
        self._src = _FileRange(path)
        directory = _run(locate_directory(self._src, PACKAGE_LIMITS))
        entries: list[Entry] = []
        it = iter_central_directory(self._src, PACKAGE_LIMITS, directory)
        while True:
            try:
                entries.append(_run(it.__anext__()))
            except StopAsyncIteration:
                break
        self._entries: dict[str, Entry] = {}
        folded: set[str] = set()
        for e in entries:
            key = fold_name(e.name)
            if key in folded:
                raise ArchiveError(ArchiveErrorCode.DUPLICATE_NAME, repr(e.name))
            folded.add(key)
            if not e.is_dir:
                self._entries[e.name] = e
        # each entry's data must end before the next header in local-header order (no overlap)
        ordered = sorted(entries, key=lambda e: e.local_header_offset)
        self._ends = {
            e.name: (
                ordered[i + 1].local_header_offset if i + 1 < len(ordered) else directory.cd_offset
            )
            for i, e in enumerate(ordered)
        }

    def names(self) -> list[str]:
        return sorted(self._entries)

    def exists(self, name: str) -> bool:
        return name in self._entries

    def chunks(self, name: str) -> Iterator[bytes]:
        entry = self._entries[name]
        gen = cast(
            "AsyncGenerator[bytes]",
            open_entry(self._src, entry, PACKAGE_LIMITS, data_end_limit=self._ends[name]),
        )
        try:
            while True:
                try:
                    yield _run(gen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            _run(gen.aclose())

    def stored_range(self, name: str) -> StoredRange:
        """Random access to an entry's bytes, which must be STORED (as our writer writes them) and
        lie before the next entry. Its CRC-32 and hash are checked when it is streamed (``chunks``)."""
        entry = self._entries[name]
        if entry.method != STORED or entry.compressed_size != entry.uncompressed_size:
            raise ArchiveError(ArchiveErrorCode.METHOD, f"{name}: not stored")
        start = _run(data_offset(self._src, entry, PACKAGE_LIMITS))
        if start + entry.compressed_size > self._ends[name]:
            raise ArchiveError(ArchiveErrorCode.OVERLAP, f"{name} overlaps the next entry")
        return StoredRange(self._src, start, entry.compressed_size)

    def close(self) -> None:
        self._src.close()


def is_zip(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return path.is_file() and fh.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def open_source(path: Path) -> DirectorySource | ZipSource:
    return ZipSource(path) if is_zip(path) else DirectorySource(path)
