"""Cursor pagination for every list endpoint (keyset, never offsets).

The cursor is opaque to clients: base64url of ``{"p": <position>, "f": <filter digest>}``. The
position is the last row's key (an id, a text key, or a composite); rows that existed when the first
page was read are returned exactly once across pages, whatever is inserted meanwhile.

The filter digest binds a cursor to the request that produced it (M17 plan §3.2): the route
template, its path parameters and every query parameter except ``cursor`` and ``limit``. A cursor
presented with other filters, another path or on another route is 422 ``cursor_mismatch``: a
silently shifted window would skip rows. :func:`bind_cursors` (an app-wide dependency) computes the
digest of the current request; :func:`encode` and :func:`decode` use it, so every list route is
bound without passing anything. Cursors stay unsigned: they are positions, and every page re-applies
tenant RLS and the caller's visibility, so a forged cursor only moves the window within what the
caller may see.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from typing import Annotated, Any

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict

from edisc_api.errors import ApiError, unprocessable
from edisc_core.canonical import canonical_json

_BINDING: ContextVar[str] = ContextVar("edisc_cursor_binding", default="")
UNBOUND = frozenset({"cursor", "limit"})


class Page[T](BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[T]
    next_cursor: str | None


async def bind_cursors(request: Request) -> None:
    """App-wide dependency: the filter digest of this request (route template, path parameters,
    query parameters but ``cursor`` and ``limit``). Async, so the context variable is set in the
    request's own context (a sync dependency runs in a thread with a copy)."""
    route = request.scope.get("route")
    doc = {
        "route": str(getattr(route, "path", "")),
        "path": sorted((k, str(v)) for k, v in request.path_params.items()),
        "query": sorted((k, v) for k, v in request.query_params.multi_items() if k not in UNBOUND),
    }
    _BINDING.set(hashlib.sha256(canonical_json(doc)).hexdigest()[:32])


def _encode(position: Any) -> str:
    raw = json.dumps({"p": position, "f": _BINDING.get()}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str) -> Any:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        doc = json.loads(base64.urlsafe_b64decode(padded))
        position, bound = doc["p"], doc["f"]
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise unprocessable("invalid cursor") from exc
    if bound != _BINDING.get():
        raise ApiError(
            422, "cursor_mismatch", "the cursor belongs to another route or other filters"
        )
    return position


def encode(last_id: uuid.UUID) -> str:
    return _encode(str(last_id))


def decode(cursor: str | None) -> uuid.UUID | None:
    if not cursor:
        return None
    position = _decode(cursor)
    try:
        return uuid.UUID(position)
    except (ValueError, TypeError, AttributeError) as exc:
        raise unprocessable("invalid cursor") from exc


CursorQ = Annotated[str | None, Query(description="opaque cursor from the previous page")]
LimitQ = Annotated[int, Query(ge=1, le=200)]


def page_of[T](rows: list[T], limit: int, key: Callable[[T], uuid.UUID]) -> Page[T]:
    """``rows`` were fetched with ``LIMIT limit + 1``: the extra row only signals another page."""
    if len(rows) > limit:
        return Page[T](items=rows[:limit], next_cursor=encode(key(rows[limit - 1])))
    return Page[T](items=rows, next_cursor=None)


def encode_text(last: str) -> str:
    return _encode(last)


def decode_text(cursor: str | None) -> str | None:
    """Keyset cursor over a text key (e.g. unit keys)."""
    if not cursor:
        return None
    value = _decode(cursor)
    if not isinstance(value, str):
        raise unprocessable("invalid cursor")
    return value


def encode_keys(*keys: str) -> str:
    """A composite position (e.g. ``created_at`` + id, or a scope type + id), as strings."""
    return _encode(list(keys))


def decode_keys(cursor: str | None, n: int) -> tuple[str, ...] | None:
    """The ``n`` string keys of :func:`encode_keys`."""
    if not cursor:
        return None
    value = _decode(cursor)
    if not isinstance(value, list) or len(value) != n or not all(isinstance(v, str) for v in value):
        raise unprocessable("invalid cursor")
    return tuple(value)
