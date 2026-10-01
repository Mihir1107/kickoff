"""Determinism: recorded histories replay against the current workflow code (ADR 0012 section 6).

Histories are recorded from real runs by the integration tests (``EDISC_RECORD_HISTORIES=1 make
test-integration TESTS=tests/integration/worker``) into ``tests/golden/temporal``. A history stays here
until Temporal visibility shows no open workflow started before the change that would break it
(``scripts/temporal_patch_check.py``). No server is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from edisc_worker.workflows import CollectionJobWorkflow, CollectUnitWorkflow

from .patched_workflows import PatchedChange, UnpatchedChange

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "temporal"
FILES = sorted(GOLDEN.glob("*.json"))


def load(path: Path) -> WorkflowHistory:
    doc = json.loads(path.read_text())
    return WorkflowHistory.from_json(doc["workflow_id"], doc["history"])


def event_types(h: WorkflowHistory) -> set[str]:
    return {e.WhichOneof("attributes") or "" for e in h.events}


def test_goldens_cover_the_required_scenarios() -> None:
    names = {p.name.rsplit("-", 1)[0] for p in FILES}
    assert {"job-clean", "job-retries", "job-cancel", "job-auth-pause"} <= names
    histories = [load(p) for p in FILES]
    parents = [h for h in histories if "/" not in h.workflow_id]
    children = [h for h in histories if "/" in h.workflow_id]
    can = "workflow_execution_continued_as_new_event_attributes"
    assert any(can in event_types(h) for h in parents), "no parent continue-as-new recorded"
    assert any(can in event_types(h) for h in children), "no child continue-as-new recorded"
    assert any("workflow_execution_signaled_event_attributes" in event_types(h) for h in parents)


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
async def test_recorded_history_replays_against_current_code(path: Path) -> None:
    await Replayer(workflows=[CollectionJobWorkflow, CollectUnitWorkflow]).replay_workflow(
        load(path)
    )


# ---------------------------------------------------------------- versioning policy (patch test)
# A change that adds a command (here: a timer before the first collect_pages) breaks replay of an
# in-flight history unless it is behind workflow.patched().


def _child_history() -> WorkflowHistory:
    children = [load(p) for p in FILES if "/" in json.loads(p.read_text())["workflow_id"]]
    assert children
    return children[0]


async def test_patched_change_replays_an_in_flight_history() -> None:
    await Replayer(workflows=[CollectionJobWorkflow, PatchedChange]).replay_workflow(
        _child_history()
    )


async def test_unpatched_change_fails_replay_negative_control() -> None:
    with pytest.raises(workflow.NondeterminismError):
        await Replayer(workflows=[CollectionJobWorkflow, UnpatchedChange]).replay_workflow(
            _child_history()
        )
