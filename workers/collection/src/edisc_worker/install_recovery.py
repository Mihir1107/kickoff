"""The token journal's reconciler, with installs finished (M17 step 6 fixes; plan §2.5, §8).

``reconcile_tokens`` runs ``connection_tokens.reconcile_token_refreshes`` (at worker start and on the
maintenance schedule) with a finisher for journaled INSTALLS: a crash after the install's tokens
were journaled and before they were applied leaves the install consumed without an outcome and the
connection pending. In the reconciler's transaction, with the tokens applied:

- the install completes exactly as the API would have done it (``complete_install``: identity,
  grant, status active, outcome ``completed``) and ``audit.oauth_install_completed`` is appended to
  the tenant's audit stream with the journaled payload plus ``recovered: true`` (actor: this
  reconciler; the acting principal is in the payload);
- a re-authorization resumes the connection's paused jobs (``job_resumed`` in each chain);
- a superseded entry (the connection's tokens moved on) ends the install ``failed``
  (``recovery_superseded``), audited.

The journal entry is deleted in that transaction (``connection_tokens``), so a second run, or a
second worker starting at the same moment (both queue on the connection row lock), finds nothing:
exactly once. Anchors are written after the commit.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import anchor_if_due, append
from edisc_db.connection_tokens import reconcile_token_refreshes
from edisc_db.install_completion import InstallFacts, complete_install, fail_install
from edisc_worker.pipeline import resume_paused_jobs

ACTOR = "system:token-reconciler"


class _Finisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.streams: list[tuple[uuid.UUID, uuid.UUID]] = []  # (tenant, stream) to anchor

    async def __call__(
        self, s: AsyncSession, tenant_id: uuid.UUID, journal: dict[str, Any], state: str
    ) -> None:
        facts = InstallFacts.from_journal(journal)
        extra = {"recovered": True, "principal_id": str(facts.principal_id),
                 **({"request_id": facts.request_id} if facts.request_id else {})}  # fmt: skip
        if state == "applied":
            if not await complete_install(s, facts):
                return  # already has an outcome: nothing to finish
            event, payload = "audit.oauth_install_completed", {**facts.audit, **extra}
        else:
            if not await fail_install(s, facts, "recovery_superseded"):
                return
            base: dict[str, Any] = {k: facts.audit[k] for k in ("provider", "install_id", "client_id",
                                                "connection_id", "reauth") if k in facts.audit}  # fmt: skip
            event, payload = "audit.oauth_install_failed", {
                **base, "detail": "recovery_superseded", **extra}  # fmt: skip
        await append(s, tenant_id=tenant_id, stream_id=tenant_id, event_type=event, actor=ACTOR,
                     payload=payload)  # fmt: skip
        self.streams.append((tenant_id, tenant_id))
        if state == "applied" and facts.reauth:
            jobs = await resume_paused_jobs(
                s, tenant_id=tenant_id, connection_id=facts.connection_id,
                actor=f"user:{facts.principal_id}",
                anchor_every=self.settings.custody_anchor_every_n_batches,
            )  # fmt: skip
            self.streams += [(tenant_id, job) for job in jobs]


async def reconcile_tokens(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
) -> dict[str, int]:
    finisher = _Finisher(settings)
    counts = await reconcile_token_refreshes(sweeper_sessions, sessions, finish_install=finisher)
    for tenant_id, stream_id in dict.fromkeys(finisher.streams):
        await anchor_if_due(sessions, s3, settings, tenant_id=tenant_id, stream_id=stream_id)
    return counts
