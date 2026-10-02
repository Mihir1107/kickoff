"""Reading entries of a LOCKED export (ADR 0014 sections 2 and 3, R6): the pinned zip version, the
directory rows written at validation, and the hardened reader. Shared by the connector, the evidence
read path and validation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.archive import (
    ArchiveLimits,
    Entry,
    EntryDigest,
    NameEncoding,
    open_entry,
    read_entry,
)
from edisc_db.session import tenant_tx
from edisc_evidence.archive_source import CoalescingSource, S3ObjectSource

LIMIT_NAMES = (
    "max_archive_bytes",
    "max_entries",
    "max_entry_bytes",
    "max_total_bytes",
    "max_total_ratio",
    "max_entry_ratio",
    "ratio_floor_bytes",
    "max_name_bytes",
)


def default_limits(settings: Settings) -> dict[str, int]:
    return {name: int(getattr(settings, f"export_{name}")) for name in LIMIT_NAMES}


def archive_limits(limits: Mapping[str, Any]) -> ArchiveLimits:
    return ArchiveLimits(**{name: int(limits[name]) for name in LIMIT_NAMES})


@dataclass(frozen=True)
class LockedExport:
    export_id: uuid.UUID
    status: str
    evidence_id: uuid.UUID
    storage_key: str
    version_id: str
    size: int
    limits: ArchiveLimits
    workspace_id: str | None
    tier: str | None
    tier_confirmed: bool | None
    findings: Mapping[str, Any]


async def load_export(session: AsyncSession, export_id: uuid.UUID) -> LockedExport:
    row = (
        await session.execute(
            text(
                "SELECT x.id, x.status, x.limits, x.workspace_id, x.detected_tier, x.tier_confirmed,"
                " x.findings, e.id AS evidence_id, e.storage_key, e.version_id, e.size_bytes"
                " FROM slack_exports x JOIN evidence_objects e ON e.id = x.evidence_object_id"
                " WHERE x.id = :i"
            ),
            {"i": export_id},
        )
    ).one()
    return LockedExport(
        row.id, row.status, row.evidence_id, row.storage_key, row.version_id, row.size_bytes,
        archive_limits(row.limits), row.workspace_id, row.detected_tier, row.tier_confirmed,
        row.findings or {},
    )  # fmt: skip


ENTRY_COLUMNS = (
    "idx, name, kind, method, flags, crc32, compressed_size, uncompressed_size, local_header_offset,"
    " raw_name, name_encoding"
)


def entry_from_row(row: Any) -> Entry:
    """The reader's ``Entry`` rebuilt from an ``export_entries`` row (all fields come from the central
    directory as validated; the local header is checked again on every read)."""
    return Entry(
        index=int(row.idx),
        name=row.name,
        is_dir=row.kind == "directory",
        method=int(row.method),
        flags=int(row.flags),
        crc32=int(row.crc32),
        compressed_size=int(row.compressed_size),
        uncompressed_size=int(row.uncompressed_size),
        local_header_offset=int(row.local_header_offset),
        raw_name=bytes(row.raw_name),
        name_encoding=NameEncoding(row.name_encoding),
    )


def archive_source(s3: S3Client, settings: Settings, export: LockedExport) -> CoalescingSource:
    return CoalescingSource(
        S3ObjectSource(
            s3,
            bucket=settings.s3_evidence_bucket,
            key=export.storage_key,
            version_id=export.version_id,
            size=export.size,
        ),
        window=settings.export_read_window_bytes,
    )


async def read_verified(
    src: CoalescingSource, entry: Entry, export: LockedExport
) -> tuple[bytes, EntryDigest]:
    """The whole entry, decompressed under the export's limits; CRC-32 and size checked, SHA-256
    computed. Day files are small; the per-entry limit bounds memory."""
    return await read_entry(src, entry, export.limits)


class ArchiveEntryIntegrityError(RuntimeError):
    """An archive entry read now does not match what was recorded at collection."""


async def open_archive_entry(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    evidence_id: uuid.UUID,
) -> AsyncIterator[bytes]:
    """Stream an ``archive_entry`` evidence object: the entry decompressed from its archive's pinned
    version, local header, CRC-32 and size checked; the SHA-256 must equal the recorded one (checked
    when the stream ends, raising if not)."""
    async with tenant_tx(sessions, tenant_id) as s:
        ev = (
            await s.execute(
                text(
                    "SELECT sha256, size_bytes, archive_evidence_id, entry_raw_name FROM evidence_objects"
                    " WHERE id = :e AND kind = 'archive_entry' AND state = 'complete'"
                ),
                {"e": evidence_id},
            )
        ).one()
        export_id: uuid.UUID = (
            await s.execute(
                text(
                    "SELECT id FROM slack_exports WHERE evidence_object_id = :a ORDER BY id LIMIT 1"
                ),
                {"a": ev.archive_evidence_id},
            )
        ).scalar_one()
        export = await load_export(s, export_id)
        row = (
            await s.execute(
                text(
                    "SELECT idx, name, kind, method, flags, crc32, compressed_size, uncompressed_size,"
                    " local_header_offset, raw_name, name_encoding FROM export_entries"
                    " WHERE export_id = :x AND raw_name = :r"
                ),
                {"x": export_id, "r": ev.entry_raw_name},
            )
        ).one()
    digests: list[EntryDigest] = []
    async for chunk in open_entry(
        archive_source(s3, settings, export), entry_from_row(row), export.limits,
        on_digest=digests.append,
    ):  # fmt: skip
        yield chunk
    if not digests or (digests[0].sha256, digests[0].size) != (ev.sha256, ev.size_bytes):
        raise ArchiveEntryIntegrityError(f"archive entry {evidence_id} differs from its record")
