"""A collection report's custody records (ADR 0018 §9): its stream's event types and the Merkle root
over its file records. Pure (no DB, no S3): the offline verifier recomputes the same root.

``report_generated`` lists every stored file as a record of ``FILE_FIELDS`` (ord, name, media type,
SHA-256, size, rows, VersionId), in ord order; its ``files_root`` is the RFC 6962 root over the
canonical JSON of those records, so a changed, dropped, added or reordered file changes the root.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from edisc_core.canonical import canonical_json
from edisc_custody.merkle import merkle_root

REPORT_STARTED = "report_started"
REPORT_GENERATED = "report_generated"  # a lifecycle event (edisc_custody.chain.LIFECYCLE_EVENTS)
REPORT_REFUSED = "report_refused"
REPORT_FAILED = "report_failed"

FILE_FIELDS = ("ord", "name", "media_type", "sha256", "size", "rows", "version_id")


def file_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in FILE_FIELDS}


def files_root(records: Iterable[Mapping[str, Any]]) -> str:
    return merkle_root(canonical_json(file_record(r)) for r in records)
