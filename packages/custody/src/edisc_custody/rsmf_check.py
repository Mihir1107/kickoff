"""Read the native references of an `.rsmf` file without loading it (ADR 0015 §20.6). Pure: standard
library and ``edisc_custody.archive`` only, for the offline verifier.

An `.rsmf` is our EML: headers, a base64 text summary, then ``rsmf.zip`` in base64 lines of 76
characters plus CRLF, then the closing boundary. ``Base64Zip`` gives the hardened zip reader random
access to the DECODED zip by mapping each range to the base64 lines that hold it, so only the zip's
directory, the manifest and the small placeholders are ever read; the evidence entries are not.

``external_refs`` reads the manifest's ``edisc.file_external`` references (``<file id>:
sha256:<hex>``) and checks each one's ``<file id>_EXTERNAL.txt`` placeholder (pinned layout: name,
size, sha256, reason, native; LF line endings) against it. Anything else raises ``RsmfCheckError``.
"""

from __future__ import annotations

import binascii
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from edisc_custody.archive import (
    ArchiveError,
    ArchiveLimits,
    iter_central_directory,
    locate_directory,
    read_entry,
)

_LINE = 76  # base64 characters per line
_STRIDE = _LINE + 2  # plus CRLF
_PER_LINE = 57  # decoded bytes per full line
_ATTACHMENT = b'Content-Disposition: attachment; filename="rsmf.zip"\r\n\r\n'
_BOUNDARY = re.compile(rb'Content-Type: multipart/mixed; boundary="([^"\r\n]{1,200})"\r\n')
_HEAD_LIMIT = 256 << 20  # headers and the summary precede the zip
_MANIFEST = "rsmf_manifest.json"
_EXTERNAL = re.compile(r"^([A-Za-z0-9_-]{1,64}): sha256:([0-9a-f]{64})$")
_PLACEHOLDER_FIELDS = ("name", "size", "sha256", "reason", "native")
_REASONS = ("over_external_threshold", "exceeds_rsmf_zip_limit")
_PLACEHOLDER_MAX = 64 << 10
# the zip of one `.rsmf`: no ZIP64 is ever written, and entries are what the renderer plans
RSMF_LIMITS = ArchiveLimits(
    max_archive_bytes=1 << 32,
    max_entries=0xFFFF,
    max_entry_bytes=1 << 32,
    max_total_bytes=1 << 40,
    max_total_ratio=1 << 20,
    max_entry_ratio=1 << 20,
)
MANIFEST_MAX = 1 << 30


class RsmfCheckError(ValueError):
    """The `.rsmf` is not the structure we write, or its native references are inconsistent."""


class RangeReader(Protocol):
    size: int

    def read(self, offset: int, length: int) -> bytes: ...


class FileRange:
    def __init__(self, path: Path) -> None:
        self._fh = path.open("rb")
        self.size = path.stat().st_size

    def read(self, offset: int, length: int) -> bytes:
        self._fh.seek(offset)
        return self._fh.read(length)

    def close(self) -> None:
        self._fh.close()


@dataclass
class SubRange:
    """``size`` bytes of ``base`` from ``start`` (a STORED entry inside a package zip)."""

    base: RangeReader
    start: int
    size: int

    def read(self, offset: int, length: int) -> bytes:
        length = max(0, min(length, self.size - offset))
        return self.base.read(self.start + offset, length) if length else b""


class Base64Zip:
    """``archive.Source`` over the decoded ``rsmf.zip`` part. Every line it touches must be exactly
    76 base64 characters and CRLF (the last one fewer), decoded strictly."""

    def __init__(self, reader: RangeReader) -> None:
        self._r = reader
        head = b""
        while _ATTACHMENT not in head:
            if len(head) >= min(_HEAD_LIMIT, reader.size):
                raise RsmfCheckError("no rsmf.zip part")
            head += reader.read(len(head), 1 << 20)
        boundary = _BOUNDARY.search(head)
        if boundary is None:
            raise RsmfCheckError("no multipart boundary")
        tail = b"\r\n--" + boundary.group(1) + b"--\r\n"
        if reader.read(reader.size - len(tail), len(tail)) != tail:
            raise RsmfCheckError("the file does not end with the closing boundary")
        self._start = head.index(_ATTACHMENT) + len(_ATTACHMENT)
        body = reader.size - len(tail) + 2 - self._start  # the base64 lines, each with its CRLF
        self._lines = -(-body // _STRIDE)
        self._last_width = body - (self._lines - 1) * _STRIDE
        if body <= 0 or self._last_width < 6:  # at least one base64 group and its CRLF
            raise RsmfCheckError("empty or truncated rsmf.zip part")
        last = self._line(self._lines - 1)
        self.size = (self._lines - 1) * _PER_LINE + len(last)

    def _decode(self, first: int, last: int) -> bytes:
        """Lines ``first..last`` (inclusive), read in one go and checked line by line."""
        end = (last - first) * _STRIDE + (self._last_width if last == self._lines - 1 else _STRIDE)
        raw = self._r.read(self._start + first * _STRIDE, end)
        if len(raw) != end:
            raise RsmfCheckError(f"base64 lines {first}..{last}: truncated")
        out = []
        for index in range(first, last + 1):
            line = raw[(index - first) * _STRIDE : (index - first) * _STRIDE + _STRIDE]
            if index == self._lines - 1:
                line = line[: self._last_width]
            if b"\r\n" in line[:-2] or not line.endswith(b"\r\n"):
                raise RsmfCheckError(f"base64 line {index}: not {_LINE} characters and CRLF")
            try:
                decoded = binascii.a2b_base64(line[:-2], strict_mode=True)
            except binascii.Error as exc:
                raise RsmfCheckError(f"base64 line {index}: {exc}") from exc
            if index != self._lines - 1 and len(decoded) != _PER_LINE:
                raise RsmfCheckError(f"base64 line {index}: padded before the last line")
            out.append(decoded)
        return b"".join(out)

    def _line(self, index: int) -> bytes:
        return self._decode(index, index)

    async def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise RsmfCheckError(f"read {offset}+{length} beyond the zip ({self.size})")
        if not length:
            return b""
        first, last = offset // _PER_LINE, (offset + length - 1) // _PER_LINE
        out = self._decode(first, last)
        skip = offset - first * _PER_LINE
        return out[skip : skip + length]


def _run[T](awaitable: Awaitable[T]) -> T:
    steps = awaitable.__await__()
    try:
        next(steps)
    except StopIteration as done:
        return cast("T", done.value)
    raise RuntimeError("rsmf read suspended: not a synchronous source")


@dataclass(frozen=True)
class ExternalRef:
    file_id: str
    sha256: str
    size: int  # from the placeholder


def external_refs(reader: RangeReader) -> list[ExternalRef]:
    """Every attachment of the `.rsmf` kept outside its zip, by file id, each checked against its
    placeholder. Raises ``RsmfCheckError`` (or ``ArchiveError`` for a damaged zip)."""
    src = Base64Zip(reader)
    directory = _run(locate_directory(src, RSMF_LIMITS))
    entries = {}
    it = iter_central_directory(src, RSMF_LIMITS, directory)
    while True:
        try:
            e = _run(it.__anext__())
        except StopAsyncIteration:
            break
        if e.name in entries:
            raise RsmfCheckError(f"zip entry {e.name} twice")
        entries[e.name] = e
    manifest_entry = entries.get(_MANIFEST)
    if manifest_entry is None or manifest_entry.uncompressed_size > MANIFEST_MAX:
        raise RsmfCheckError("no readable manifest")
    data, _ = _run(read_entry(src, manifest_entry, RSMF_LIMITS))
    try:
        manifest = json.loads(data)
        values = [
            str(pair["value"])
            for event in manifest["events"]
            for pair in event.get("custom", [])
            if pair.get("name") == "edisc.file_external"
        ]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RsmfCheckError(f"manifest: {exc}") from exc
    refs: dict[str, ExternalRef] = {}
    for value in values:
        match = _EXTERNAL.match(value)
        if match is None:
            raise RsmfCheckError(f"edisc.file_external {value!r} is malformed")
        fid, sha = match.groups()
        if fid in refs:
            if refs[fid].sha256 != sha:
                raise RsmfCheckError(f"file {fid} references two natives")
            continue
        refs[fid] = ExternalRef(fid, sha, _placeholder(src, entries, fid, sha))
    placeholders = sorted(n for n in entries if n.endswith("_EXTERNAL.txt"))
    if placeholders != sorted(f"{fid}_EXTERNAL.txt" for fid in refs):
        raise RsmfCheckError("external placeholders and references differ")
    return sorted(refs.values(), key=lambda r: r.file_id)


def _placeholder(src: Base64Zip, entries: dict[str, Any], fid: str, sha: str) -> int:
    entry = entries.get(f"{fid}_EXTERNAL.txt")
    if entry is None or entry.uncompressed_size > _PLACEHOLDER_MAX:
        raise RsmfCheckError(f"file {fid}: no placeholder for its native")
    try:
        data, _ = _run(read_entry(src, entry, RSMF_LIMITS))
        text = data.decode("utf-8")
    except (ArchiveError, UnicodeDecodeError) as exc:
        raise RsmfCheckError(f"file {fid}: placeholder unreadable: {exc}") from exc
    lines = text.split("\n")
    if lines[-1] != "" or len(lines) != len(_PLACEHOLDER_FIELDS) + 1 or "\r" in text:
        raise RsmfCheckError(f"file {fid}: placeholder layout")
    fields = {}
    for name, line in zip(_PLACEHOLDER_FIELDS, lines, strict=False):
        key, sep, value = line.partition(": ")
        if key != name or not sep:
            raise RsmfCheckError(f"file {fid}: placeholder field {name} missing")
        fields[name] = value
    if (
        fields["sha256"] != sha
        or fields["native"] != f"natives/{sha}"
        or fields["reason"] not in _REASONS
        or not fields["size"].isdigit()
    ):
        raise RsmfCheckError(f"file {fid}: placeholder disagrees with its reference")
    return int(fields["size"])
