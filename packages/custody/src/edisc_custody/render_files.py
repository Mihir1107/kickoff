"""Render output files in custody (ADR 0015 §14). Pure: stdlib + edisc_core.canonical + merkle.

A render's files are committed in bounded batches, like collection batches. Each
``render_files_batch`` event carries the RFC 6962 root over its files, in render order; the leaf of
a file is the canonical JSON of its record (``FILE_FIELDS``: name, slice, part, VersionId, SHA-256,
size, source hash and counts), so any change to a recorded file changes the root. ``render_completed``
carries the total file count, the number of batches and the root over the batch roots
(``batches_root``), so a dropped, added or reordered batch is detected too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from edisc_core.canonical import canonical_json
from edisc_custody.merkle import merkle_root

RENDER_BATCH_EVENT = "render_files_batch"
RENDER_STARTED = "render_started"
RENDER_COMPLETED = "render_completed"
RENDER_REFUSED = "render_refused"
RENDER_FAILED = "render_failed"
RENDER_LIFECYCLE = frozenset({RENDER_STARTED, RENDER_COMPLETED, RENDER_REFUSED, RENDER_FAILED})

FILE_FIELDS = (
    "ord",
    "name",
    "conversation_id",
    "day",
    "time_zone",
    "part",
    "parts",
    "version_id",
    "sha256",
    "size",
    "source_hash",
    "event_collection_id",
    "event_count",
    "context_event_count",
    "attachment_count",
    "unavailable_count",
)


class RenderFileError(ValueError):
    pass


def file_leaf_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """The hashed view of one file record. Every field is required: a missing one is an error."""
    missing = [f for f in FILE_FIELDS if f not in record]
    if missing:
        raise RenderFileError(f"file record lacks {missing}")
    return {f: record[f] for f in FILE_FIELDS}


def files_root(records: Iterable[Mapping[str, Any]]) -> str:
    """Root over one batch of file records, in render order (``ord`` strictly increasing)."""
    leaves: list[bytes] = []
    last = -1
    for rec in records:
        view = file_leaf_record(rec)
        if not isinstance(view["ord"], int) or view["ord"] <= last:
            raise RenderFileError(f"file ord {view['ord']!r} is not after {last}")
        last = view["ord"]
        leaves.append(canonical_json(view))
    return merkle_root(leaves)


def batches_root(roots: Sequence[str]) -> str:
    """Root over the batch roots, in batch order (32-byte leaves)."""
    leaves = []
    for r in roots:
        try:
            raw = bytes.fromhex(r)
        except (ValueError, TypeError) as exc:
            raise RenderFileError(f"batch root {r!r} is not hex") from exc
        if len(raw) != 32:
            raise RenderFileError(f"batch root {r!r} is not 64 hex chars")
        leaves.append(raw)
    return merkle_root(leaves)
