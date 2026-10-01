"""Cursor pagination for every list endpoint: keyset on the row id, ascending.

The cursor is opaque to clients (base64url of the last id). Rows that existed when the first page was
read are returned exactly once across pages, whatever is inserted meanwhile (no offsets).
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict

from edisc_api.errors import unprocessable


class Page[T](BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[T]
    next_cursor: str | None


def encode(last_id: uuid.UUID) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps({"after": str(last_id)}).encode()).decode().rstrip("=")
    )


def decode(cursor: str | None) -> uuid.UUID | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return uuid.UUID(json.loads(base64.urlsafe_b64decode(padded))["after"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise unprocessable("invalid cursor") from exc


CursorQ = Annotated[str | None, Query(description="opaque cursor from the previous page")]
LimitQ = Annotated[int, Query(ge=1, le=200)]


def page_of[T](rows: list[T], limit: int, key: Callable[[T], uuid.UUID]) -> Page[T]:
    """``rows`` were fetched with ``LIMIT limit + 1``: the extra row only signals another page."""
    if len(rows) > limit:
        return Page[T](items=rows[:limit], next_cursor=encode(key(rows[limit - 1])))
    return Page[T](items=rows, next_cursor=None)


def encode_text(last: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"after_key": last}).encode()).decode().rstrip("=")


def decode_text(cursor: str | None) -> str | None:
    """Keyset cursor over a text key (e.g. unit keys)."""
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded))["after_key"]
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise unprocessable("invalid cursor") from exc
    if not isinstance(value, str):
        raise unprocessable("invalid cursor")
    return value
