"""Retention extension (ADR 0002 amendment): keep evidence locked while something still needs it.

Objects are written with a short rolling COMPLIANCE window. This job pushes it forward, using the same
floor/target rule as dedup hits (``extension_needed``):

- **Matter evidence:** every complete object referenced by an ACTIVE matter (not closed, retention date
  not passed): its jobs' own objects (pages, files, seals, anchors) and every object behind an item one of
  its jobs linked (dedup across matters). Target: ``min(now + window, latest retention_until of the active
  matters referencing it)``.
- **Client-level evidence:** validated (``ready``) Slack exports while their client is ACTIVE (not
  closed). They belong to no matter until a job uses them. Target: ``now + window``.

Nothing is ever shortened. Closing the matter (or client) stops extension and objects expire on schedule.
Each tenant with extensions gets one ``audit.retention_extended`` event per run. Cross-tenant: the sweeper
login lists tenant ids only; all reads and writes run as the app role inside each tenant's transaction.
Failures are collected and raised together after every object was attempted.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now
from edisc_custody.log import anchor_if_due, append
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_evidence.writer import EvidenceWriter

ACTOR = "system:retention"

MATTER_CANDIDATES = (
    "WITH active AS (SELECT id, retention_until FROM matters"
    "  WHERE closed_at IS NULL AND retention_until > :now),"
    " refs AS ("
    "  SELECT e.id, m.retention_until FROM active m JOIN collection_jobs j ON j.matter_id = m.id"
    "   JOIN evidence_objects e ON e.job_id = j.id"
    "  UNION ALL"
    "  SELECT i.evidence_object_id, m.retention_until FROM active m"
    "   JOIN collection_jobs j ON j.matter_id = m.id JOIN job_items ji ON ji.job_id = j.id"
    "   JOIN items i ON i.id = ji.item_id)"
    " SELECT e.id, e.storage_key, e.version_id, e.retain_until, max(r.retention_until) AS until"
    " FROM refs r JOIN evidence_objects e ON e.id = r.id"
    " WHERE e.state = 'complete' AND e.retain_until < :need"
    " AND (CAST(:after AS uuid) IS NULL OR e.id > :after)"
    " GROUP BY e.id ORDER BY e.id LIMIT :n"
)
EXPORT_CANDIDATES = (
    "SELECT e.id, e.storage_key, e.version_id, e.retain_until, NULL::timestamptz AS until"
    " FROM slack_exports x JOIN clients c ON c.id = x.client_id"
    " JOIN evidence_objects e ON e.id = x.evidence_object_id"
    " WHERE x.status = 'ready' AND c.closed_at IS NULL AND e.state = 'complete'"
    " AND e.retain_until < :need AND (CAST(:after AS uuid) IS NULL OR e.id > :after)"
    " GROUP BY e.id ORDER BY e.id LIMIT :n"
)
SOURCES = {"matter": MATTER_CANDIDATES, "export": EXPORT_CANDIDATES}


@dataclass
class ExtensionResult:
    tenants: int = 0
    examined: Counter[str] = field(default_factory=Counter)
    extended: Counter[str] = field(default_factory=Counter)


class RetentionExtensionError(ExceptionGroup[Exception]):
    pass


async def extend_retention(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    now: datetime | None = None,
    tenant_id: uuid.UUID | None = None,
    page: int = 1000,
) -> ExtensionResult:
    """``now`` exists for simulated-time tests; production passes nothing."""
    now = ensure_utc(now) if now is not None else utc_now()
    need = now + timedelta(days=settings.evidence_retention_extend_floor_days)
    if tenant_id is not None:
        tenants: Sequence[uuid.UUID] = [tenant_id]
    else:
        async with sweeper_sessions() as session, session.begin():
            tenants = (
                (await session.execute(text("SELECT id FROM tenants ORDER BY id"))).scalars().all()
            )
    writer = EvidenceWriter(sessions, s3, settings)
    result = ExtensionResult(tenants=len(tenants))
    failures: list[Exception] = []
    for tenant in tenants:
        extended: Counter[str] = Counter()
        for source, sql in SOURCES.items():
            after: uuid.UUID | None = None
            while True:
                async with tenant_tx(sessions, tenant) as s:
                    rows = (
                        await s.execute(
                            text(sql), {"now": now, "need": need, "after": after, "n": page}
                        )
                    ).all()
                for row in rows:
                    result.examined[source] += 1
                    try:
                        target = effective_retain_until(settings, row.until, now=now)
                        if await writer.extend_retention(
                            tenant, row.id, row.storage_key, row.version_id, row.retain_until,
                            target, now=now,
                        ):  # fmt: skip
                            extended[source] += 1
                    except Exception as exc:  # noqa: BLE001 - collected and re-raised below
                        exc.add_note(f"extending evidence {row.id} of tenant {tenant}")
                        failures.append(exc)
                if len(rows) < page:
                    break
                after = rows[-1].id
        if extended:
            result.extended.update(extended)
            await _record(sessions, s3, settings, tenant, now, extended)
    if failures:
        raise RetentionExtensionError(f"{len(failures)} retention extensions failed", failures)
    return result


async def _record(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    tenant: uuid.UUID,
    now: datetime,
    extended: Counter[str],
) -> None:
    payload: dict[str, Any] = {
        "matter_evidence": extended["matter"],
        "export_evidence": extended["export"],
        "as_of": now.isoformat(),
        "window_days": settings.evidence_retention_window_days,
        "floor_days": str(settings.evidence_retention_extend_floor_days),  # no floats in custody
    }
    async with tenant_tx(sessions, tenant) as s:
        await append(
            s,
            tenant_id=tenant,
            stream_id=tenant,
            event_type="audit.retention_extended",
            actor=ACTOR,
            payload=payload,
        )
    await anchor_if_due(sessions, s3, settings, tenant_id=tenant, stream_id=tenant)
