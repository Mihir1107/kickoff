"""Anchor sweeper: seals overdue chain heads that no writer will ever come back for.

Two identities: ``sweeper_sessions`` (the ``edisc_sweeper`` login, the only role allowed to call
``due_anchor_streams``) finds overdue streams across tenants; ``sessions`` (the app role) anchors each
one inside its own tenant transaction.

A stream is swept when ``anchor_due`` is set (a lifecycle event or threshold whose anchor was never
written, e.g. the worker died) or when its unanchored tail has been idle for
``custody_anchor_sweep_idle_seconds`` (e.g. an abandoned job). Run periodically (Temporal schedule, M12).

Failures are never swallowed: every stream is attempted, then all failures are raised together.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import anchor_if_due


@dataclass(frozen=True)
class SweepResult:
    anchored: list[str]
    streams_seen: int


class SweepError(ExceptionGroup[Exception]):
    pass


async def sweep_anchors(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    idle: timedelta | None = None,
    limit: int = 500,
    tenant_id: uuid.UUID | None = None,
) -> SweepResult:
    idle = (
        idle if idle is not None else timedelta(seconds=settings.custody_anchor_sweep_idle_seconds)
    )
    # Cross-tenant lookup (ids only) as the sweeper login; anchoring below as the app role, per tenant.
    async with sweeper_sessions() as session, session.begin():
        rows = (
            await session.execute(
                text("SELECT tenant_id, stream_id FROM due_anchor_streams(:idle, :limit, :tenant)"),
                {"idle": idle, "limit": limit, "tenant": tenant_id},
            )
        ).all()
    anchored: list[str] = []
    failures: list[Exception] = []
    for row in rows:
        try:
            key = await anchor_if_due(
                sessions,
                s3,
                settings,
                tenant_id=row.tenant_id,
                stream_id=row.stream_id,
                force=True,
                wait=False,  # a live claim is someone else's anchor in progress; a stale one is taken over
            )
        except Exception as exc:  # noqa: BLE001 - collected and re-raised below, never swallowed
            exc.add_note(f"sweeping stream {row.stream_id} of tenant {row.tenant_id}")
            failures.append(exc)
            continue
        if key:
            anchored.append(key)
    if failures:
        raise SweepError(f"{len(failures)} of {len(rows)} overdue anchors failed", failures)
    return SweepResult(anchored=anchored, streams_seen=len(rows))
