"""Legal-hold sweeps (maintenance; M18 §1.3, §1.4).

- ``flag-held-objects`` (15 min): starts ``LegalHoldWorkflow`` for every hold with work to do: an
  active hold whose last pass finished (objects linked since then get their flag: new jobs,
  renders, reports, dedup hits), one never flagged, or a released one not yet cleared. A workflow
  already running for a hold is left alone (``USE_EXISTING``).
- ``expire-hold-releases`` (15 min): a release request past its 72 hours is recorded as expired
  (``audit.legal_hold_release_expired`` + an alert), so the chain shows it when it happened, not
  only when someone next tries to approve.

The sweeper login lists tenant ids only; all reads and writes run as the app role per tenant.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.log import anchor_if_due, append
from edisc_db.session import tenant_tx
from edisc_worker.contracts import HOLDS_QUEUE, hold_workflow_id

ACTOR = "system:legal-hold"


async def _tenants(
    sweeper_sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID | None
) -> list[uuid.UUID]:
    if tenant_id is not None:
        return [tenant_id]
    async with sweeper_sessions() as session, session.begin():
        return list((await session.execute(text("SELECT id FROM tenants ORDER BY id"))).scalars())


async def start_flag_passes(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    temporal: Client,
    *,
    tenant_id: uuid.UUID | None = None,
) -> int:
    started = 0
    for tenant in await _tenants(sweeper_sessions, tenant_id):
        async with tenant_tx(sessions, tenant) as s:
            holds: list[uuid.UUID] = list(
                (await s.execute(text(
                    "SELECT id FROM legal_holds WHERE (released_at IS NULL)"
                    " OR (released_at IS NOT NULL AND flag_state <> 'cleared') ORDER BY id"
                ))).scalars()
            )  # fmt: skip
        for hold in holds:
            await temporal.start_workflow(
                "LegalHoldWorkflow",
                args=[str(tenant), str(hold)],
                id=hold_workflow_id(hold),
                task_queue=HOLDS_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            started += 1
    return started


async def expire_release_requests(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID | None = None,
) -> int:
    expired = 0
    for tenant in await _tenants(sweeper_sessions, tenant_id):
        async with tenant_tx(sessions, tenant) as s:
            rows = (
                await s.execute(
                    text(
                        "UPDATE legal_hold_release_requests SET decision = 'expired', decided_at = now()"
                        " WHERE decision IS NULL AND expires_at <= now()"
                        " RETURNING id, hold_id, requested_by"
                    )
                )
            ).all()
            for row in rows:
                await append(
                    s, tenant_id=tenant, stream_id=tenant, actor=ACTOR,
                    event_type="audit.legal_hold_release_expired",
                    payload={"hold_id": str(row.hold_id), "request_id": str(row.id),
                             "requested_by": row.requested_by},
                )  # fmt: skip
                await s.execute(
                    text("INSERT INTO alerts (id, tenant_id, kind, message) VALUES (:i, :t, :k, :m)"),
                    {"i": new_id(), "t": tenant, "k": "legal_hold_release_expired",
                     "m": f"the request to release legal hold {row.hold_id} expired without a"
                          " second approval"},
                )  # fmt: skip
        if rows:
            expired += len(rows)
            await anchor_if_due(sessions, s3, settings, tenant_id=tenant, stream_id=tenant)
    return expired
