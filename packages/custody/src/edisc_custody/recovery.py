"""Evidence recovery with custody: every recovery outcome is recorded in the job's chain.

- ``recover_job_evidence``: runs ``EvidenceWriter.recover_pending`` and appends one
  ``evidence_recovered`` event with the outcome (recovery path ``persisted_source_hash``).
- ``complete_by_refetch``: for objects stored without a persisted source hash. Re-reads the SOURCE,
  compares, completes, and appends an ``evidence_recovered`` event with recovery path
  ``refetch_from_source``. Storage-derived hashes never stand in for collection-time hashes.

Both are lifecycle events, so the chain head is anchored to WORM right after.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import anchor_if_due, append
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter, RecoveryReport, WrittenEvidence

RECOVERED = "evidence_recovered"


async def _record(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    actor: str,
    payload: dict[str, object],
) -> None:
    async with tenant_tx(sessions, tenant_id) as s:
        await append(
            s,
            tenant_id=tenant_id,
            stream_id=job_id,
            job_id=job_id,
            event_type=RECOVERED,
            actor=actor,
            payload=payload,
            anchor_every=settings.custody_anchor_every_n_batches,
        )
    await anchor_if_due(sessions, s3, settings, tenant_id=tenant_id, stream_id=job_id)


async def recover_job_evidence(
    writer: EvidenceWriter,
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    actor: str,
) -> RecoveryReport:
    report = await writer.recover_pending(tenant_id=tenant_id, job_id=job_id)
    if report.completed or report.missing or report.left_pending or report.needs_refetch:
        await _record(
            sessions,
            s3,
            settings,
            tenant_id=tenant_id,
            job_id=job_id,
            actor=actor,
            payload={
                "recovery_path": "persisted_source_hash",
                "completed": sorted(str(i) for i in report.completed),
                "missing": sorted(str(i) for i in report.missing),
                "left_pending": sorted(str(i) for i in report.left_pending),
                "needs_refetch": sorted(str(i) for i in report.needs_refetch),
                "shadow_versions": {str(k): sorted(v) for k, v in report.shadows.items()},
            },
        )
    return report


async def complete_by_refetch(
    writer: EvidenceWriter,
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    evidence_id: uuid.UUID,
    source: AsyncIterable[bytes],
    actor: str,
) -> WrittenEvidence:
    written = await writer.complete_by_refetch(
        tenant_id=tenant_id, evidence_id=evidence_id, source=source
    )
    await _record(
        sessions,
        s3,
        settings,
        tenant_id=tenant_id,
        job_id=job_id,
        actor=actor,
        payload={
            "recovery_path": "refetch_from_source",
            "evidence_id": str(evidence_id),
            "storage_key": written.storage_key,
            "version_id": written.version_id,
            "sha256": written.sha256,
            "size": written.size,
        },
    )
    return written
