"""scripts/temporal_patch_check.py finds open workflows started before a patch deploy (R5)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from types import ModuleType

from temporalio.client import Client

from edisc_core.time import utc_now
from edisc_worker.contracts import JobInput
from edisc_worker.workflows import CollectionJobWorkflow

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "temporal_patch_check.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("temporal_patch_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_open_workflows_started_before_the_deploy_block_patch_removal(
    temporal: Client,
) -> None:
    check = _script()
    job_id = uuid.uuid4()
    before_start = utc_now() - timedelta(seconds=5)
    # no worker polls this queue: the workflow stays open, like a multi-day job
    handle = await temporal.start_workflow(
        CollectionJobWorkflow.run,
        JobInput(str(uuid.uuid4()), str(job_id)),
        id=str(job_id),
        task_queue=f"no-worker-{job_id.hex}",
    )
    try:
        deployed_after = utc_now() + timedelta(seconds=5)
        found: list[str] = []
        for _ in range(50):  # visibility is eventually consistent
            found = await check.open_before(temporal, ["CollectionJobWorkflow"], deployed_after)
            if any(str(job_id) in f for f in found):
                break
            await asyncio.sleep(0.2)
        assert any(str(job_id) in f for f in found)
        earlier = await check.open_before(temporal, ["CollectionJobWorkflow"], before_start)
        assert not any(str(job_id) in f for f in earlier)
    finally:
        await handle.terminate("test cleanup")
