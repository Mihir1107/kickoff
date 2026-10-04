"""The RSMF envelope (ADR 0015 §5): RFC 5322 with CRLF, MIME multipart/mixed, a text/plain summary and
`rsmf.zip` in base64. Headers are folded at 78 characters, and non-ASCII values use RFC 2047
encoded-words split on code-point boundaries. Everything is a pure function of its arguments."""

from __future__ import annotations

import binascii
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime

from edisc_core.time import ensure_utc

CRLF = b"\r\n"
LINE = 78
HARD_LINE = 998
_B64_IN = 57  # 57 input bytes -> 76 base64 characters per line
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def rfc5322_date(value: datetime) -> str:
    """`Mon, 05 Jan 2026 23:59:59 +0000`, without locale or clock."""
    v = ensure_utc(value)
    return (
        f"{_DAYS[v.weekday()]}, {v.day:02d} {_MONTHS[v.month - 1]} {v.year:04d} "
        f"{v.hour:02d}:{v.minute:02d}:{v.second:02d} +0000"
    )


def _plain_ascii(name: str, value: str) -> bool:
    """Written as-is (folded at spaces): printable ASCII, single spaces between words, nothing that
    looks like an encoded-word. Folding happens only at spaces, so one long word (a Message-ID, a
    hash) can make its line longer than 78 but never longer than the hard limit of 998."""
    if not value or not all(" " <= c <= "~" for c in value) or "=?" in value:
        return False
    words = value.split(" ")
    return all(words) and len(name) + 2 + max(len(w) for w in words) <= HARD_LINE


def _fold_ascii(name: str, value: str) -> list[str]:
    words = value.split(" ")
    lines: list[str] = []
    line = f"{name}: {words[0]}"
    for w in words[1:]:
        if len(line) + 1 + len(w) <= LINE:
            line += " " + w
        else:
            lines.append(line)
            line = " " + w
    lines.append(line)
    return lines


def _encoded_words(name: str, value: str) -> list[str]:
    """RFC 2047 B encoding in UTF-8. Each line holds one encoded-word of at most 75 characters, and
    adjacent words are separated by folding white space, which decoders drop."""
    lines: list[str] = []
    prefix = f"{name}: "
    chars = list(value)
    i = 0
    while i < len(chars):
        overhead = len("=?utf-8?b??=")
        room = LINE - len(prefix) - overhead
        max_bytes = min(room // 4 * 3, (75 - overhead) // 4 * 3)
        if max_bytes < 4:  # header name too long to share a line with any encoded-word
            lines.append(prefix.rstrip())
            prefix = " "
            continue
        chunk = b""
        while i < len(chars):
            nxt = chars[i].encode("utf-8")
            if len(chunk) + len(nxt) > max_bytes:
                break
            chunk += nxt
            i += 1
        word = "=?utf-8?b?" + binascii.b2a_base64(chunk, newline=False).decode("ascii") + "?="
        lines.append(prefix + word)
        prefix = " "
    return lines


def header(name: str, value: str) -> bytes:
    lines = _fold_ascii(name, value) if _plain_ascii(name, value) else _encoded_words(name, value)
    return b"".join(line.encode("ascii") + CRLF for line in lines)


def base64_lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Base64 in 76-character CRLF lines, streamed: holds at most one partial line plus a chunk."""
    buffer = bytearray()
    for chunk in chunks:
        buffer += chunk
        whole = len(buffer) // _B64_IN * _B64_IN
        if whole:
            view = bytes(buffer[:whole])
            del buffer[:whole]
            yield b"".join(
                binascii.b2a_base64(view[i : i + _B64_IN], newline=False) + CRLF
                for i in range(0, whole, _B64_IN)
            )
    if buffer:
        yield binascii.b2a_base64(bytes(buffer), newline=False) + CRLF


def envelope(
    headers: Sequence[tuple[str, str]],
    boundary: str,
    summary: str,
    zip_chunks: Iterable[bytes],
) -> Iterator[bytes]:
    """The whole EML as a stream of byte chunks."""
    head = b"".join(header(n, v) for n, v in headers)
    head += header("MIME-Version", "1.0")
    head += f'Content-Type: multipart/mixed; boundary="{boundary}"'.encode("ascii") + CRLF
    head += CRLF
    head += f"--{boundary}".encode("ascii") + CRLF
    head += b'Content-Type: text/plain; charset="utf-8"' + CRLF
    head += b"Content-Transfer-Encoding: base64" + CRLF + CRLF
    head += b"".join(base64_lines([summary.encode("utf-8")]))
    head += f"--{boundary}".encode("ascii") + CRLF
    head += b'Content-Type: application/zip; name="rsmf.zip"' + CRLF
    head += b"Content-Transfer-Encoding: base64" + CRLF
    head += b'Content-Disposition: attachment; filename="rsmf.zip"' + CRLF + CRLF
    yield head
    yield from base64_lines(zip_chunks)
    yield f"--{boundary}--".encode("ascii") + CRLF
