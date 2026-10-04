"""Names inside `rsmf.zip` (ADR 0015 §4): flat and safe, prefixed by the file id."""

from __future__ import annotations

import unicodedata

MANIFEST_NAME = "rsmf_manifest.json"
MAX_NAME_BYTES = 200
_MAX_EXT_BYTES = 16
_UNSAFE = frozenset('/\\:*?"<>|')
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"})


def _sanitize(name: str) -> str:
    """NFC, then path separators, Windows-reserved characters, control/format/private/unassigned code
    points and every space separator except U+0020 become `_`. Leading and trailing spaces and dots
    are trimmed (Windows drops them)."""
    nfc = unicodedata.normalize("NFC", name)
    out = "".join(
        "_" if c in _UNSAFE or (c != " " and unicodedata.category(c) in _UNSAFE_CATEGORIES) else c
        for c in nfc
    )
    return out.strip(" .")


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")  # never splits a code point


def attachment_name(file_id: str, original: str) -> str:
    """`{file_id}_{sanitized name}`, at most 200 UTF-8 bytes, keeping the extension."""
    safe = _sanitize(original) or "file"
    stem, dot, ext = safe.rpartition(".")
    if not dot or not stem or len(ext.encode("utf-8")) > _MAX_EXT_BYTES:
        stem, ext = safe, ""
    suffix = f".{ext}" if ext else ""
    prefix = f"{file_id}_"
    budget = MAX_NAME_BYTES - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
    return prefix + _truncate_utf8(stem, budget) + suffix


PLACEHOLDER_SUFFIX = ".UNAVAILABLE.txt"


def placeholder_name(file_id: str, original: str) -> str:
    """The attachment's own safe name plus `.UNAVAILABLE.txt`, still within 200 bytes:
    `F1_report.pdf.UNAVAILABLE.txt`."""
    name = attachment_name(file_id, original)
    budget = MAX_NAME_BYTES - len(PLACEHOLDER_SUFFIX.encode("utf-8"))
    return _truncate_utf8(name, budget) + PLACEHOLDER_SUFFIX
