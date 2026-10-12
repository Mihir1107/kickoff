"""Archive entries in a custody package (format ``/2``, ADR 0014 section 2). Pure: standard library
and ``edisc_custody.archive`` only, so the offline verifier stays free of database and cloud code.

For each archive the manifest lists (embedded in ``objects/`` or referenced by hash and supplied with
``--archive``):

1. its SHA-256 and size are checked FIRST; an archive that is missing or does not match is reported and
   none of its entries is opened;
2. the central directory is streamed once: every entry the package references is found by its EXACT
   name bytes, and a second entry with the same (case/Unicode-folded) name is a duplicate error;
3. each entry's recorded CRC-32 and compressed size must equal the directory's; it is then decompressed
   under the export's recorded limits with the local header, overlap bound, CRC-32 and size checked, and
   its SHA-256 and size must equal the evidence record.

Item fragments are then checked against the entry bytes (``body``) like any page.

An embedded archive is read where it lies: a file of a directory package, or a STORED entry of a zip
package (random access into the entry's bytes; nothing is copied out).
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import concurrent.futures
import hashlib
import os
from array import array
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from edisc_custody.archive import (
    ArchiveError,
    ArchiveLimits,
    Entry,
    fold_name,
    iter_central_directory,
    locate_directory,
    open_entry,
)
from edisc_custody.package_source import DirectorySource, PackageSource, StoredRange, ZipSource

if TYPE_CHECKING:
    from edisc_custody.package import PackageReport

LIMIT_FIELDS = (
    "max_archive_bytes",
    "max_entries",
    "max_entry_bytes",
    "max_total_bytes",
    "max_total_ratio",
    "max_entry_ratio",
    "ratio_floor_bytes",
    "max_name_bytes",
)


class ArchiveSource(Protocol):
    size: int

    async def read(self, offset: int, length: int) -> bytes: ...

    def close(self) -> None: ...


class FileSource:
    """Random access to a local archive file (the reader's ``Source``)."""

    def __init__(self, path: Path) -> None:
        self._fd = os.open(path, os.O_RDONLY)
        self.size = os.fstat(self._fd).st_size

    async def read(self, offset: int, length: int) -> bytes:
        return os.pread(self._fd, length, offset)

    def close(self) -> None:
        os.close(self._fd)


class EntrySource:
    """Random access to an archive embedded as a STORED entry of a zip package (the reader's
    ``Source``); the entry's CRC-32 and hash are checked when it is streamed first."""

    def __init__(self, stored: StoredRange) -> None:
        self._stored, self.size = stored, stored.size

    async def read(self, offset: int, length: int) -> bytes:
        return self._stored.read(offset, length)

    def close(self) -> None:
        pass  # the package source owns the file


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run the async reader to completion from sync code, whether or not a loop is running here."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def sha256_file(path: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _sha256_member(source: PackageSource, name: str) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    for chunk in source.chunks(name):
        h.update(chunk)
        size += len(chunk)
    return h.hexdigest(), size


def _limits(recorded: Mapping[str, Any] | None) -> ArchiveLimits:
    if not recorded:
        return ArchiveLimits()
    return ArchiveLimits(**{k: int(recorded[k]) for k in LIMIT_FIELDS if k in recorded})


class ArchiveEntries:
    def __init__(
        self,
        source: PackageSource,
        manifest: Mapping[str, Any],
        evidence: Mapping[str, Mapping[str, Any]],
        supplied: Sequence[Path],
        report: PackageReport,
    ) -> None:
        self._report = report
        self._evidence = evidence
        # archive evidence id -> opener of the verified archive (embedded or supplied)
        self._opens: dict[str, Callable[[], ArchiveSource]] = {}
        self._limits: dict[str, ArchiveLimits] = {}
        self._entries: dict[str, tuple[Entry, int]] = {}  # entry evidence id -> (entry, data end)
        self._cache: tuple[str, bytes] | None = None
        archives = list(manifest.get("archives") or [])
        if not archives:
            for rec in evidence.values():
                if rec.get("kind") == "archive_entry":
                    report.errors.append(
                        f"evidence {rec['storage_key']}: archive entry without an archive in the manifest"
                    )
            return
        by_hash: dict[str, Path] = {}
        for path in supplied:  # hashed once each; matched by content, never by name
            digest, _ = sha256_file(path)
            by_hash.setdefault(digest, path)
        for archive in archives:
            self._open_archive(source, archive, by_hash)
        for archive_id in list(self._opens):
            self._verify_entries(archive_id)

    # ------------------------------------------------------------------ archives
    def _open_archive(
        self, source: PackageSource, archive: Mapping[str, Any], by_hash: Mapping[str, Path]
    ) -> None:
        report, sha, size = self._report, str(archive["sha256"]), int(archive["size_bytes"])
        where = f"archive {archive.get('storage_key')} ({sha})"
        opener: Callable[[], ArchiveSource]
        if archive.get("embedded"):
            name = f"objects/{sha}"
            if not source.exists(name):
                report.errors.append(f"{where}: embedded archive missing from package")
                return
            digest, actual = _sha256_member(source, name)  # before any entry is read
            opener = _embedded(source, name)
        else:
            supplied = by_hash.get(sha)
            if supplied is None:
                report.errors.append(
                    f"{where}: referenced by hash; supply the archive with --archive (no entry was verified)"
                )
                return
            digest, actual = sha256_file(supplied)  # the archive itself, before any entry is read
            opener = _file(supplied)
        if (digest, actual) != (sha, size):
            report.errors.append(
                f"{where}: SHA-256/size do not match the record (got {digest}, {actual} bytes); "
                "no entry was verified"
            )
            return
        try:
            opener().close()  # a zip package: the entry must be STORED and in its bounds
        except ArchiveError as exc:
            report.errors.append(f"{where}: {exc}; no entry was verified")
            return
        report.archives_checked += 1
        self._opens[str(archive["evidence_id"])] = opener
        self._limits[str(archive["evidence_id"])] = _limits(archive.get("limits"))

    def _verify_entries(self, archive_id: str) -> None:
        wanted = {
            base64.b64decode(rec["entry_raw_name_b64"]): rec
            for rec in self._evidence.values()
            if rec.get("kind") == "archive_entry"
            and str(rec.get("archive_evidence_id")) == archive_id
        }
        if not wanted:
            return
        src = self._opens[archive_id]()
        try:
            _run(self._scan(src, wanted, self._limits[archive_id]))
        finally:
            src.close()

    async def _scan(
        self, src: ArchiveSource, wanted: Mapping[bytes, Mapping[str, Any]], limits: ArchiveLimits
    ) -> None:
        report = self._report
        try:
            directory = await locate_directory(src, limits)
            folded_wanted = {fold_name(rec["entry_path"]) for rec in wanted.values()}
            seen_folded: dict[str, int] = {}
            found: dict[bytes, Entry] = {}
            offsets = array("Q")  # every local header offset (8 bytes per entry): overlap bounds
            async for e in iter_central_directory(src, limits, directory):
                offsets.append(e.local_header_offset)
                key = fold_name(e.name)
                if key in folded_wanted:
                    seen_folded[key] = seen_folded.get(key, 0) + 1
                if e.raw_name in wanted:
                    found[e.raw_name] = e
        except ArchiveError as exc:
            report.errors.append(f"archive: unreadable central directory: {exc}")
            return
        bounds = sorted(offsets)
        for raw, rec in wanted.items():
            where = f"entry {rec['entry_path']!r}"
            entry = found.get(raw)
            if entry is None:
                report.errors.append(f"{where}: not in the archive's central directory")
                continue
            if seen_folded.get(fold_name(entry.name), 0) > 1:
                report.errors.append(f"{where}: duplicate name in the archive")
                continue
            if (entry.crc32, entry.compressed_size) != (
                int(rec["entry_crc32"]),
                int(rec["entry_compressed_size"]),
            ):
                report.errors.append(f"{where}: CRC-32/compressed size differ from the record")
                continue
            i = bisect.bisect_right(bounds, entry.local_header_offset)
            end = bounds[i] if i < len(bounds) else directory.cd_offset
            try:
                digest = await _digest(src, entry, limits, end)
            except ArchiveError as exc:
                report.errors.append(f"{where}: {exc}")
                continue
            if (digest[0], digest[1]) != (rec["sha256"], int(rec["size_bytes"])):
                report.errors.append(f"{where}: decompressed SHA-256/size differ from the record")
                continue
            report.entries_checked += 1
            self._entries[str(rec["id"])] = (entry, end)

    # ------------------------------------------------------------------ item checks
    def body(self, evidence_id: str) -> bytes | None:
        """The verified entry's bytes (read again, re-checked), or None if it did not verify."""
        if self._cache is not None and self._cache[0] == evidence_id:
            return self._cache[1]
        located = self._entries.get(evidence_id)
        if located is None:
            return None
        rec = self._evidence[evidence_id]
        archive_id = str(rec["archive_evidence_id"])
        src = self._opens[archive_id]()
        try:
            data, digest = _run(_read(src, located[0], self._limits[archive_id], located[1]))
        except ArchiveError as exc:
            self._report.errors.append(f"entry {rec['entry_path']!r}: {exc}")
            return None
        finally:
            src.close()
        if digest != rec["sha256"]:
            self._report.errors.append(f"entry {rec['entry_path']!r}: changed while verifying")
            return None
        self._cache = (evidence_id, data)
        return data


def _embedded(source: PackageSource, name: str) -> Callable[[], ArchiveSource]:
    if isinstance(source, ZipSource):
        return lambda: EntrySource(source.stored_range(name))
    if isinstance(source, DirectorySource):
        path = source.root / name
        return lambda: FileSource(path)
    raise TypeError(f"unsupported package source {source!r}")


def _file(path: Path) -> Callable[[], ArchiveSource]:
    return lambda: FileSource(path)


async def _digest(
    src: ArchiveSource, entry: Entry, limits: ArchiveLimits, end: int
) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    async for chunk in open_entry(src, entry, limits, data_end_limit=end):
        h.update(chunk)
        size += len(chunk)
    return h.hexdigest(), size


async def _read(
    src: ArchiveSource, entry: Entry, limits: ArchiveLimits, end: int
) -> tuple[bytes, str]:
    parts = [c async for c in open_entry(src, entry, limits, data_end_limit=end)]
    data = b"".join(parts)
    return data, hashlib.sha256(data).hexdigest()
