"""RFC 8785 JSON Canonicalization Scheme (JCS) and hashing helpers.

Every hash that must be reproducible (custody events, version fingerprints, raw item hashes, Merkle
leaves) goes through :func:`canonical_json`. Never hash ``json.dumps`` output.

Domain values are converted to JSON first by :func:`to_jsonable` with fixed rules:
UUID → lowercase hyphenated string; datetime → ``format_utc`` (naive rejected); date → ISO date;
bytes → rejected (hash them and store the hex instead); Enum → its value; Pydantic model → ``model_dump(mode="json")``.
Strings are NOT Unicode-normalized (ADR 0004).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

import rfc8785
from pydantic import BaseModel

from edisc_core.time import format_utc

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
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump(mode="json"))
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


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    """SHA-256 hex of the canonical JSON of ``value``."""
    return sha256_hex(canonical_json(value))
