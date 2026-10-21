"""Lone UTF-16 surrogates in source strings, represented losslessly AND validly (ADR 0018 §23).

A Python ``str`` can hold a lone surrogate (U+D800-U+DFFF on its own) when a source's JSON escapes one
(``"\\ud800"``): JSON allows the escape, but UTF-8, Postgres text/jsonb and RFC 8785 cannot carry the
code point. Raw evidence keeps the source's bytes and source hashes use
`edisc_verify.canonical.canonical_source_json` (lossless). Everything we STORE or PUBLISH (derived
records, report files) uses this representation instead:

- the string itself becomes its **replacement text**: each lone surrogate replaced by U+FFFD (one
  character for one, so positions are kept);
- an explicit field records the **exact UTF-16 code units** of the original string, keyed by the
  string's RFC 6901 JSON Pointer inside the record: ``{"/text": [104, 105, 55357]}``.

`make_valid` builds both; `restore` turns them back into the exact strings. Nothing is guessed: a
record without the field holds no lone surrogate, and a pointer whose replacement text does not match
its code units is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REPLACEMENT = "�"
FIELD = "lone_surrogates"  # the explicit field's name in derived records and report rows


def has_lone(text: str) -> bool:
    """True when ``text`` holds a surrogate code point (in a ``str``, every surrogate is lone)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def utf16_units(text: str) -> list[int]:
    """The UTF-16 code units of ``text``, lone surrogates as themselves."""
    data = text.encode("utf-16-be", "surrogatepass")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def from_utf16_units(units: list[int]) -> str:
    """The string whose UTF-16 code units these are (a high unit before a low one joins them)."""
    if any(not isinstance(u, int) or isinstance(u, bool) or not 0 <= u <= 0xFFFF for u in units):
        raise ValueError("UTF-16 code units are integers 0..65535")
    return b"".join(u.to_bytes(2, "big") for u in units).decode("utf-16-be", "surrogatepass")


def replacement(text: str) -> str:
    """``text`` with every lone surrogate replaced by U+FFFD (one for one)."""
    if not has_lone(text):
        return text
    return "".join(REPLACEMENT if 0xD800 <= ord(c) <= 0xDFFF else c for c in text)


def recorded_text(text: str, limit: int) -> tuple[str, dict[str, list[int]]]:
    """A free text we record (an error text, which can quote source data) cut to ``limit`` code
    points: its valid form, and ``{"utf16": code units}`` when it held a lone surrogate (else empty;
    a record adds it as ``<field>_utf16``). Recording a failure must never fail on what it quotes."""
    cut = text[:limit]
    return replacement(cut), ({"utf16": utf16_units(cut)} if has_lone(cut) else {})


SUBSTITUTE_PREFIX = "lone-surrogate:"


def substitute_key(value: str) -> str:
    """A stand-in key for a STRUCTURAL source value holding a lone surrogate (a conversation id),
    which can be neither stored nor keyed (ADR 0018 §28): ``lone-surrogate:`` + its exact UTF-16 code
    units in hex, dot-separated. Lossless, ASCII, no ``/``, never equal to a source id. Used only to
    name the unit that reports the value as a gap; nothing is collected under it."""
    return SUBSTITUTE_PREFIX + ".".join(f"{u:04x}" for u in utf16_units(value))


def _pointer_token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def make_valid(value: Any, pointer: str = "") -> tuple[Any, dict[str, list[int]]]:
    """``value`` (JSON-like: dicts with str keys, lists, tuples, scalars) with every string holding a
    lone surrogate replaced by its replacement text, and ``{pointer: code units}`` for each of them.
    Returns ``value`` itself when nothing was found. Keys must be valid: a record's keys are ours, a
    key with a lone surrogate is refused (``ValueError``) rather than merged with another."""
    found: dict[str, list[int]] = {}
    out = _walk(value, pointer, found)
    return (value if not found else out), found


def _walk(value: Any, pointer: str, found: dict[str, list[int]]) -> Any:
    if isinstance(value, str):
        if has_lone(value):
            found[pointer] = utf16_units(value)
            return replacement(value)
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and has_lone(k):
                raise ValueError(f"a key with a lone surrogate at {pointer!r}: {k!a}")
            out[k] = _walk(v, f"{pointer}/{_pointer_token(str(k))}", found)
        return out
    if isinstance(value, list | tuple):
        return [_walk(v, f"{pointer}/{i}", found) for i, v in enumerate(value)]
    return value


def restore(value: Any, found: Mapping[str, list[int]]) -> Any:
    """The exact strings back: each pointer's replacement text is checked against its code units
    and replaced by the original. Refuses (``ValueError``) a pointer that names no string or whose
    text is not the replacement of its code units."""
    out = value
    for pointer, units in sorted(found.items()):
        original = from_utf16_units(list(units))
        out = _set(out, _tokens(pointer), original, pointer)
    return out


def _tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON Pointer: {pointer!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer[1:].split("/")]


def _set(value: Any, tokens: list[str], original: str, pointer: str) -> Any:
    if not tokens:
        if not isinstance(value, str) or value != replacement(original):
            raise ValueError(f"{pointer}: the text is not the replacement of its code units")
        return original
    head, rest = tokens[0], tokens[1:]
    if isinstance(value, Mapping):
        if head not in value:
            raise ValueError(f"{pointer}: no such field")
        return {**value, head: _set(value[head], rest, original, pointer)}
    if isinstance(value, list | tuple):
        if not head.isdigit() or int(head) >= len(value):
            raise ValueError(f"{pointer}: no such element")
        items = list(value)
        items[int(head)] = _set(items[int(head)], rest, original, pointer)
        return items
    raise ValueError(f"{pointer}: no such field")
