"""Audit events (ADR 0013 section 4): an append-only hash chain per tenant.

They reuse the custody chain machinery with stream id = tenant id, so they are append-only, hash-chained
and anchored to WORM exactly like job custody. Job-scoped actions (start, cancel, resume, rerun) go to
the job's own custody stream instead (with the same actor naming).

Call ``record`` INSIDE the tenant transaction that makes the change, then ``anchor`` after commit.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import anchor_if_due, append


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: str,
    event_type: str,
    payload: dict[str, Any],
    request_id: str | None = None,
) -> None:
    await append(
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
