"""Audit events (ADR 0013 section 4): an append-only hash chain per tenant.

They reuse the custody chain machinery with stream id = tenant id, so they are append-only, hash-chained
and anchored to WORM exactly like job custody. Job-scoped actions (start, cancel, resume, rerun) go to
the job's own custody stream instead (with the same actor naming).

Call ``record`` INSIDE the tenant transaction that makes the change, then ``anchor`` after commit.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import AppendedEvent, anchor_if_due, append
from edisc_db.session import tenant_tx


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: str,
    event_type: str,
    payload: dict[str, Any],
    request_id: str | None = None,
) -> AppendedEvent:
    return await append(
        session,
        tenant_id=tenant_id,
        stream_id=tenant_id,
        event_type=f"audit.{event_type}",
        actor=actor,
        payload={**payload, **({"request_id": request_id} if request_id else {})},
    )


async def anchor(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    tenant_id: uuid.UUID,
) -> None:
    await anchor_if_due(sessions, s3, settings, tenant_id=tenant_id, stream_id=tenant_id)


async def anchor_now(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    tenant_id: uuid.UUID,
    event: AppendedEvent,
) -> None:
    """Anchor the tenant's audit stream NOW (forced), and make sure the anchor covers ``event``:
    for reads whose audit must be in WORM before any byte leaves (render packages)."""
    await anchor_if_due(
        sessions, s3, settings, tenant_id=tenant_id, stream_id=tenant_id, force=True
    )
    async with tenant_tx(sessions, tenant_id) as s:
        anchored: int = (
            await s.execute(
                text("SELECT last_anchored_seq FROM custody_chain_heads WHERE stream_id = :s"),
                {"s": tenant_id},
            )
        ).scalar_one()
    if anchored < event.seq:
        raise RuntimeError(f"audit event {event.seq} is not anchored (anchored up to {anchored})")
