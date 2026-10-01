"""Variants of CollectUnitWorkflow for the versioning-policy test. A separate module because the
Temporal sandbox re-imports a workflow's module and forbids I/O at import time."""

from __future__ import annotations

from temporalio import workflow

from edisc_worker.contracts import UnitInput
from edisc_worker.workflows import CollectUnitWorkflow


@workflow.defn(name="CollectUnitWorkflow")
class UnpatchedChange(CollectUnitWorkflow):
    @workflow.run
    async def run(self, inp: UnitInput) -> str:
        await workflow.sleep(0.01)
        return await super().run(inp)


@workflow.defn(name="CollectUnitWorkflow")
class PatchedChange(CollectUnitWorkflow):
    @workflow.run
    async def run(self, inp: UnitInput) -> str:
        if workflow.patched("example-pre-collect-timer"):
            await workflow.sleep(0.01)
        return await super().run(inp)
