"""May a workflow patch (and its golden histories) be removed yet? ADR 0012 section 6, R5.

    uv run python scripts/temporal_patch_check.py --deployed-at 2026-10-01T12:00:00Z \
        [--workflow-type CollectUnitWorkflow --workflow-type CollectionJobWorkflow]

Lists RUNNING workflows of the given types started before the patch was deployed. Continue-as-new
runs count from their own start time, so a long chain that continued after the deploy runs new code.
Exit 0: none (safe to ``deprecate_patch`` and later delete the old branch and its goldens).
Exit 1: some remain (keep the patch and the histories). No time-based shortcut.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from temporalio.client import Client

from edisc_core.settings import Settings
from edisc_core.time import ensure_utc

DEFAULT_TYPES = ["CollectionJobWorkflow", "CollectUnitWorkflow"]


def query(workflow_type: str, deployed_at: datetime) -> str:
    stamp = ensure_utc(deployed_at).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"WorkflowType = '{workflow_type}' AND ExecutionStatus = 'Running' "
        f"AND StartTime < '{stamp}'"
    )


async def open_before(client: Client, types: list[str], deployed_at: datetime) -> list[str]:
    found: list[str] = []
    for wf_type in types:
        found.extend(
            [
                f"{wf.workflow_type} {wf.id} run={wf.run_id} started={wf.start_time.isoformat()}"
                async for wf in client.list_workflows(query(wf_type, deployed_at))
            ]
        )
    return found


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--deployed-at", required=True, type=datetime.fromisoformat)
    ap.add_argument("--workflow-type", action="append", dest="types")
    args = ap.parse_args()
    settings = Settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    remaining = await open_before(client, args.types or DEFAULT_TYPES, args.deployed_at)
    for line in remaining:
        print(line)
    if remaining:
        print(f"{len(remaining)} workflow(s) started before the patch are still open: keep it")
        return 1
    print("no open workflow started before the patch: it may be deprecated")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
