"""Activities of ``LegalHoldWorkflow`` (queue ``holds``; M18 §1.3). The worker that runs them holds
the HOLD storage identity for flags and the app identity for anchors, nothing else."""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity

from edisc_worker.holds import HoldFlagger


class HoldActivities:
    def __init__(self, flagger: HoldFlagger) -> None:
        self.flagger = flagger

    @activity.defn(name="hold_begin_pass")
    async def hold_begin_pass(self, tenant_id: str, hold_id: str) -> str:
        return await self.flagger.begin(uuid.UUID(tenant_id), uuid.UUID(hold_id))

    @activity.defn(name="hold_step")
    async def hold_step(self, tenant_id: str, hold_id: str) -> bool:
        return await self.flagger.step(uuid.UUID(tenant_id), uuid.UUID(hold_id))

    def all(self) -> list[Any]:
        return [self.hold_begin_pass, self.hold_step]
