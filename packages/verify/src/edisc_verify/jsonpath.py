"""Minimal JSON path for item pointers into raw pages: ``$``, ``.name``, ``["name"]``, ``[index]``.

Deliberately tiny and exact (no wildcards, filters or slices), so a third-party verifier can
re-implement it trivially. ``build`` and ``resolve`` round-trip for any key.
"""

from __future__ import annotations

import json
import re
from typing import Any

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class JsonPathError(ValueError):
    pass


def build(*segments: str | int) -> str:
    out = "$"
    for seg in segments:
        if isinstance(seg, bool):
            raise JsonPathError("bool is not a path segment")
        if isinstance(seg, int):
            if seg < 0:
                raise JsonPathError("negative index")
            out += f"[{seg}]"
        elif _IDENT.fullmatch(seg):
            out += f".{seg}"
        else:
            out += f"[{json.dumps(seg, ensure_ascii=False)}]"
    return out


def parse(path: str) -> list[str | int]:
    if not path.startswith("$"):
        raise JsonPathError(f"path must start with $: {path!r}")
    segments: list[str | int] = []
    i, n = 1, len(path)
    decoder = json.JSONDecoder()
    while i < n:
        if path[i] == ".":
            m = _IDENT.match(path, i + 1)
            if not m:
                raise JsonPathError(f"bad name at {i} in {path!r}")
            segments.append(m.group())
            i = m.end()
        elif path[i] == "[":
            if i + 1 < n and path[i + 1] == '"':
                try:
                    key, end = decoder.raw_decode(path, i + 1)
                except ValueError as exc:
                    raise JsonPathError(f"bad quoted key at {i} in {path!r}") from exc
                if not isinstance(key, str) or end >= n or path[end] != "]":
                    raise JsonPathError(f"bad quoted key at {i} in {path!r}")
                segments.append(key)
                i = end + 1
            else:
                end = path.find("]", i)
                digits = path[i + 1 : end] if end != -1 else ""
                if not digits.isdigit() or (len(digits) > 1 and digits[0] == "0"):
                    raise JsonPathError(f"bad index at {i} in {path!r}")
                segments.append(int(digits))
                i = end + 1
        else:
            raise JsonPathError(f"unexpected {path[i]!r} at {i} in {path!r}")
    return segments


def resolve(document: Any, path: str) -> Any:
    node = document
    for seg in parse(path):
        if isinstance(seg, int):
            if not isinstance(node, list) or seg >= len(node):
                raise JsonPathError(f"index {seg} not found in {path!r}")
        elif not isinstance(node, dict) or seg not in node:
            raise JsonPathError(f"key {seg!r} not found in {path!r}")
        node = node[seg]
    return node
