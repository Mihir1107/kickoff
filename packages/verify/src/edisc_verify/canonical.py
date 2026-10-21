"""RFC 8785 JSON Canonicalization Scheme (JCS) and hashing helpers.

Every hash that must be reproducible (custody events, version fingerprints, raw item hashes, Merkle
leaves) goes through :func:`canonical_json`. Never hash ``json.dumps`` output.

Domain values are converted to JSON first by :func:`to_jsonable` with fixed rules:
UUID → lowercase hyphenated string; datetime → ``format_utc`` (naive rejected); date → ISO date;
bytes → rejected (hash them and store the hex instead); Enum → its value; a Pydantic model (anything
with ``model_dump``: the verifier does not depend on Pydantic) → ``model_dump(mode="json")``.
Strings are NOT Unicode-normalized (ADR 0004).

SOURCE data (raw items, fingerprints: what a source sent) is hashed with :func:`canonical_source_json`:
the same bytes for every value :func:`canonical_json` accepts, and a lone UTF-16 surrogate (a str can
hold one when a source's JSON escapes it, ``"\\ud800"``) written as that escape instead of failing
the job (ADR 0018 §23). Everything else stays strict: a lone surrogate must never reach a custody
event, a stored record or a report as a raw code point (see :mod:`edisc_verify.lone_surrogates`).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

import rfc8785

from edisc_verify.utc import format_utc

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class CanonicalizationError(ValueError):
    pass


def to_jsonable(value: Any) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return format_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if callable(getattr(value, "model_dump", None)) and not isinstance(value, Mapping):
        return to_jsonable(value.model_dump(mode="json"))  # a Pydantic model (product types)
    if isinstance(value, Mapping):
        out: dict[str, JsonValue] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError(f"object keys must be str, got {type(k).__name__}")
            out[k] = to_jsonable(v)
        return out
    if isinstance(value, bytes | bytearray | memoryview):
        raise CanonicalizationError("bytes are not JSON; hash them and include the hex digest")
    if isinstance(value, Sequence):
        return [to_jsonable(v) for v in value]
    raise CanonicalizationError(f"unsupported type for canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """RFC 8785 canonical UTF-8 bytes. Raises on NaN/Infinity, lone surrogates, non-str keys."""
    try:
        return rfc8785.dumps(to_jsonable(value))
    except rfc8785.CanonicalizationError as exc:
        raise CanonicalizationError(str(exc)) from exc
    except UnicodeEncodeError as exc:  # rfc8785 sorts keys by UTF-16: a lone surrogate key
        raise CanonicalizationError("input contains non-UTF-8 codepoints") from exc


def canonical_source_json(value: Any) -> bytes:
    """RFC 8785 for SOURCE data, lossless for lone surrogates: byte-identical to
    :func:`canonical_json` for every value it accepts; a lone UTF-16 surrogate (in a string or a key)
    is written as its ``\\uXXXX`` escape with lowercase hex, exactly as ECMAScript's well-formed
    ``JSON.stringify`` writes it (the Node cross-check in tests/golden/jcs), and keys sort by UTF-16
    code units as RFC 8785 says. The output decodes back to the same strings. A high surrogate
    directly followed by a low one as two code points (which no JSON decoder produces: it joins their
    escapes into one character) is refused, because its escapes would decode as that character."""
    jv = to_jsonable(value)
    try:
        return rfc8785.dumps(jv)
    except (rfc8785.CanonicalizationError, UnicodeEncodeError):
        pass  # a lone surrogate somewhere, or a value no mode accepts (raised below)
    out: list[bytes] = []
    _source_dump(jv, out)
    return b"".join(out)


def canonical_source_hash(value: Any) -> str:
    """SHA-256 hex of :func:`canonical_source_json`; equal to :func:`canonical_hash` wherever that
    one accepts the value."""
    return sha256_hex(canonical_source_json(value))


def _source_dump(value: JsonValue, out: list[bytes]) -> None:
    if isinstance(value, str):
        out.append(_source_str(value))
    elif isinstance(value, list):
        out.append(b"[")
        for i, v in enumerate(value):
            if i:
                out.append(b",")
            _source_dump(v, out)
        out.append(b"]")
    elif isinstance(value, dict):
        out.append(b"{")
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be", "surrogatepass"))
        for i, (k, v) in enumerate(items):
            if i:
                out.append(b",")
            out.append(_source_str(k))
            out.append(b":")
            _source_dump(v, out)
        out.append(b"}")
    else:
        try:
            out.append(rfc8785.dumps(value))
        except rfc8785.CanonicalizationError as exc:
            raise CanonicalizationError(str(exc)) from exc


def _source_str(s: str) -> bytes:
    try:
        return rfc8785.dumps(s)
    except rfc8785.CanonicalizationError:
        pass
    parts = [b'"']
    run: list[str] = []
    high = False
    for ch in s:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            if high and cp >= 0xDC00:
                raise CanonicalizationError(
                    "a high surrogate directly followed by a low one (two code points) has no "
                    "lossless JSON form"
                )
            if run:
                parts.append(rfc8785.dumps("".join(run))[1:-1])
                run.clear()
            parts.append(b"\\u%04x" % cp)
            high = cp < 0xDC00
        else:
            run.append(ch)
            high = False
    if run:
        parts.append(rfc8785.dumps("".join(run))[1:-1])
    parts.append(b'"')
    return b"".join(parts)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    """SHA-256 hex of the canonical JSON of ``value``."""
    return sha256_hex(canonical_json(value))
