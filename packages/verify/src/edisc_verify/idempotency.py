"""Idempotency key (ADR 0004): tenant + source + source_item_id + content_hash, unit-separator joined."""

from __future__ import annotations

import hashlib
import re
import uuid

_SEP = "\x1f"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def idempotency_key(
    tenant_id: uuid.UUID | str, source: str, source_item_id: str, content_hash: str
) -> str:
    parts = [str(tenant_id), source, source_item_id, content_hash]
    if any(_SEP in p for p in parts):
        raise ValueError("idempotency key components must not contain U+001F")
    if not source or not source_item_id:
        raise ValueError("source and source_item_id are required")
    if not _HEX64.match(content_hash):
        raise ValueError("content_hash must be 64 lowercase hex chars")
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()
