"""Retention extension (ADR 0002 amendment): keep evidence locked while something still needs it.

Objects are written with a short rolling COMPLIANCE window. This job pushes it forward, using the same
floor/target rule as dedup hits (``extension_needed``):

- **Matter evidence:** every complete object referenced by an ACTIVE matter (not closed, retention date
  not passed): its jobs' own objects (pages, files, seals, anchors) and every object behind an item one of
  its jobs linked (dedup across matters). An archive entry is protected through its locked archive. Target: ``min(now + window, latest retention_until of the active
  matters referencing it)``.
- **Client-level evidence:** validated (``ready``) Slack exports while their client is ACTIVE (not
  closed). They belong to no matter until a job uses them. Target: ``now + window``.

Nothing is ever shortened. Closing the matter (or client) stops extension and objects expire on schedule.
After a reopen, an object found with LAPSED retention is re-locked if its pinned version still exists, or
reported missing (alert) if not; either way a ``retention_gaps`` row records the unprotected window and an
``audit.retention_gap`` event commits to the rows.
Each tenant with extensions gets one ``audit.retention_extended`` event per run. Cross-tenant: the sweeper
login lists tenant ids only; all reads and writes run as the app role inside each tenant's transaction.
Failures are collected and raised together after every object was attempted.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
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
    "  SELECT e.id, m.id AS matter_id, m.retention_until FROM active m"
    "   JOIN collection_jobs j ON j.matter_id = m.id"
    "   JOIN evidence_objects e ON e.job_id = j.id"
    "  UNION ALL"
    "  SELECT i.evidence_object_id, m.id, m.retention_until FROM active m"
    "   JOIN collection_jobs j ON j.matter_id = m.id JOIN job_items ji ON ji.job_id = j.id"
    "   JOIN items i ON i.id = ji.item_id),"
    # an archive entry is protected by locking its archive (the entry is never an object of its own)
    " owned AS (SELECT coalesce(x.archive_evidence_id, x.id) AS id, r.matter_id, r.retention_until"
    "  FROM refs r JOIN evidence_objects x ON x.id = r.id)"
    " SELECT e.id, e.storage_key, e.version_id, e.retain_until, max(r.retention_until) AS until,"
    " (array_agg(r.matter_id ORDER BY r.retention_until DESC))[1] AS owner"
    " FROM owned r JOIN evidence_objects e ON e.id = r.id"
    " WHERE e.state = 'complete' AND e.retain_until < :need"
    " AND (CAST(:after AS uuid) IS NULL OR e.id > :after)"
    " GROUP BY e.id ORDER BY e.id LIMIT :n"
)
EXPORT_CANDIDATES = (
    "SELECT e.id, e.storage_key, e.version_id, e.retain_until, NULL::timestamptz AS until,"
    " (array_agg(c.id ORDER BY c.id))[1] AS owner"
    " FROM slack_exports x JOIN clients c ON c.id = x.client_id"
    " JOIN evidence_objects e ON e.id = x.evidence_object_id"
    " WHERE x.status = 'ready' AND c.closed_at IS NULL AND e.state = 'complete'"
    " AND e.retain_until < :need AND (CAST(:after AS uuid) IS NULL OR e.id > :after)"
    " GROUP BY e.id ORDER BY e.id LIMIT :n"
)
SOURCES = {"matter": MATTER_CANDIDATES, "export": EXPORT_CANDIDATES}
OWNER_TYPE = {"matter": "matter", "export": "client"}


@dataclass(frozen=True)
class Gap:
    evidence_id: uuid.UUID
    owner_type: str
    owner_id: uuid.UUID
    unprotected_from: datetime
    outcome: str  # relocked | missing


@dataclass
class ExtensionResult:
    tenants: int = 0
    examined: Counter[str] = field(default_factory=Counter)
    extended: Counter[str] = field(default_factory=Counter)
    gaps: Counter[str] = field(default_factory=Counter)  # relocked | missing


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
        gaps: list[Gap] = []
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
                        lapsed = ensure_utc(row.retain_until) <= now
                        if lapsed and not await _exists(s3, settings, row):
                            gaps.append(
                                Gap(row.id, OWNER_TYPE[source], row.owner,
                                    ensure_utc(row.retain_until), "missing")
                            )  # fmt: skip
                            continue
                        target = effective_retain_until(settings, row.until, now=now)
                        if await writer.extend_retention(
                            tenant, row.id, row.storage_key, row.version_id, row.retain_until,
                            target, now=now,
                        ):  # fmt: skip
                            extended[source] += 1
                            if lapsed:  # was unprotected until now: recorded, never hidden
                                gaps.append(
                                    Gap(row.id, OWNER_TYPE[source], row.owner,
                                        ensure_utc(row.retain_until), "relocked")
                                )  # fmt: skip
                    except Exception as exc:  # noqa: BLE001 - collected and re-raised below
                        exc.add_note(f"extending evidence {row.id} of tenant {tenant}")
                        failures.append(exc)
                if len(rows) < page:
                    break
                after = rows[-1].id
        if gaps:
            result.gaps.update(g.outcome for g in gaps)
            await _record_gaps(sessions, tenant, now, gaps)
        if extended:
            result.extended.update(extended)
            await _record(sessions, s3, settings, tenant, now, extended)
        elif gaps:
            await anchor_if_due(sessions, s3, settings, tenant_id=tenant, stream_id=tenant)
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


async def _exists(s3: S3Client, settings: Settings, row: Any) -> bool:
    """Does the pinned version still exist? (Its retention lapsed, so it could have been deleted.)"""
    try:
        await s3.head_object(
            Bucket=settings.s3_evidence_bucket, Key=row.storage_key, VersionId=row.version_id
        )
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NoSuchVersion"):
            return False
        raise
    return True


async def _record_gaps(
    sessions: async_sessionmaker[AsyncSession], tenant: uuid.UUID, now: datetime, gaps: list[Gap]
) -> None:
    """Rows for every gap, one custody event committing to all of them (count, window, digest), and an
    alert for every object that is gone."""
    lines = sorted(
        f"{g.evidence_id}|{g.owner_type}|{g.owner_id}|{g.unprotected_from.isoformat()}|"
        f"{now.isoformat()}|{g.outcome}"
        for g in gaps
    )
    payload: dict[str, Any] = {
        "relocked": sum(g.outcome == "relocked" for g in gaps),
        "missing": sum(g.outcome == "missing" for g in gaps),
        "unprotected_from": min(g.unprotected_from for g in gaps).isoformat(),
        "unprotected_until": now.isoformat(),
        "owners": sorted({f"{g.owner_type}:{g.owner_id}" for g in gaps}),
        "gaps_sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
    }
    async with tenant_tx(sessions, tenant) as s:
        for g in gaps:
            await s.execute(
                text(
                    "INSERT INTO retention_gaps (id, tenant_id, evidence_object_id, owner_type, owner_id,"
                    " unprotected_from, unprotected_until, outcome)"
                    " VALUES (:i, :t, :e, :ot, :o, :f, :u, :out)"
                ),
                {"i": new_id(), "t": tenant, "e": g.evidence_id, "ot": g.owner_type,
                 "o": g.owner_id, "f": g.unprotected_from, "u": now, "out": g.outcome},
            )  # fmt: skip
            if g.outcome == "missing":
                await s.execute(
                    text(
                        "INSERT INTO alerts (id, tenant_id, kind, message)"
                        " VALUES (:i, :t, 'evidence_missing', :m)"
                    ),
                    {"i": new_id(), "t": tenant,
                     "m": f"evidence {g.evidence_id} is gone: its retention lapsed while its "
                          f"{g.owner_type} {g.owner_id} did not protect it"},
                )  # fmt: skip
        await append(
            s,
            tenant_id=tenant,
            stream_id=tenant,
            event_type="audit.retention_gap",
            actor=ACTOR,
            payload=payload,
        )
