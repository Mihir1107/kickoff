"""Render output files in custody (ADR 0015 §14). Pure: stdlib + edisc_core.canonical + merkle.

A render's files are committed in bounded batches, like collection batches. Each
``render_files_batch`` event carries the RFC 6962 root over its files, in render order; the leaf of
a file is the canonical JSON of its record (``FILE_FIELDS``: name, slice, part, VersionId, SHA-256,
size, source hash and counts), so any change to a recorded file changes the root. ``render_completed``
carries the total file count, the number of batches and the root over the batch roots
(``batches_root``), so a dropped, added or reordered batch is detected too.

From renderer 1.3.0 (ADR 0015 §20) a file record also carries ``external_count`` (``FILE_FIELDS``;
older renders keep ``FILE_FIELDS_1``, chosen by the renderer version ``render_started`` records), and
attachments kept outside the zip are natives: one record per (render, SHA-256) (``NATIVE_FIELDS``:
ord, SHA-256, size, storage key, VersionId and the ords of every file that references it), committed
with the batch of the first file that references it. Each ``render_files_batch`` then carries
``natives_root`` over that batch's native records (``native_count``, ``first_native_ord``), and
``render_completed`` the native count and ``natives_root`` over every native record in order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from typing import Any

from edisc_core.canonical import canonical_json
from edisc_custody.merkle import merkle_root

RENDER_BATCH_EVENT = "render_files_batch"
RENDER_STARTED = "render_started"
RENDER_COMPLETED = "render_completed"
RENDER_REFUSED = "render_refused"
RENDER_FAILED = "render_failed"
RENDER_LIFECYCLE = frozenset({RENDER_STARTED, RENDER_COMPLETED, RENDER_REFUSED, RENDER_FAILED})

FILE_FIELDS_1 = (
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
"""The file leaf of renders made before renderer 1.3.0."""
FILE_FIELDS = (*FILE_FIELDS_1, "external_count")
NATIVES_FROM = (1, 3, 0)
"""The first renderer version with natives and ``external_count`` (ADR 0015 §20)."""
NATIVE_FIELDS = ("ord", "sha256", "size", "storage_key", "version_id", "file_ords")


class RenderFileError(ValueError):
    pass


def has_natives(renderer_version: str | None) -> bool:
    """Whether a render of this renderer version records natives and ``external_count``."""
    if renderer_version is None:
        return False
    try:
        parts = tuple(int(p) for p in renderer_version.split("."))
    except ValueError as exc:
        raise RenderFileError(f"renderer version {renderer_version!r} is not numeric") from exc
    return parts >= NATIVES_FROM


def file_fields(renderer_version: str | None) -> tuple[str, ...]:
    return FILE_FIELDS if has_natives(renderer_version) else FILE_FIELDS_1


def file_leaf_record(
    record: Mapping[str, Any], fields: tuple[str, ...] = FILE_FIELDS
) -> dict[str, Any]:
    """The hashed view of one file record. Every field is required: a missing one is an error."""
    missing = [f for f in fields if f not in record]
    if missing:
        raise RenderFileError(f"file record lacks {missing}")
    return {f: record[f] for f in fields}


def files_root(records: Iterable[Mapping[str, Any]], fields: tuple[str, ...] = FILE_FIELDS) -> str:
    """Root over one batch of file records, in render order (``ord`` strictly increasing)."""
    leaves: list[bytes] = []
    last = -1
    for rec in records:
        view = file_leaf_record(rec, fields)
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


def native_leaf_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """The hashed view of one native record (every field required, ``file_ords`` strictly
    increasing and not empty: a native nothing references is never written)."""
    missing = [f for f in NATIVE_FIELDS if f not in record]
    if missing:
        raise RenderFileError(f"native record lacks {missing}")
    ords = record["file_ords"]
    if (
        not isinstance(ords, list)
        or not ords
        or not all(isinstance(o, int) and not isinstance(o, bool) for o in ords)
        or any(b <= a for a, b in pairwise(ords))
    ):
        raise RenderFileError(
            f"native {record.get('sha256')}: file_ords {ords!r} is not a sorted list"
        )
    return {f: record[f] for f in NATIVE_FIELDS}


def natives_root(records: Iterable[Mapping[str, Any]], first_ord: int = 0) -> str:
    """Root over native records in order: ``ord`` consecutive from ``first_ord``, each SHA-256 once."""
    leaves: list[bytes] = []
    expected = first_ord
    for rec in records:
        view = native_leaf_record(rec)
        if view["ord"] != expected:
            raise RenderFileError(f"native ord {view['ord']!r} where {expected} was expected")
        expected += 1
        leaves.append(canonical_json(view))
    return merkle_root(leaves)
