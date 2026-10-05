"""Render operations (ADR 0015 §15): stuck sealing made visible, the anchor sweeper covering render
streams, and version routing (a mixed-version worker pool never fails a render from skew)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker
from types_aiobotocore_s3 import S3Client

from edisc_api.routes.renders import _out
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc
from edisc_custody.chain import anchor_key
from edisc_custody.log import verify_chain
from edisc_custody.sweeper import sweep_anchors
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_renderers.rsmf import runtime_versions
from edisc_worker import renders
from edisc_worker.pipeline import CrashHooks
from edisc_worker.renders import (
    RenderActivities,
    RenderIntegrityError,
    RenderRun,
    create_render,
    start_render_workflow,
)
from edisc_worker.workflows import RenderWorkflow

from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..pipeline.conftest import CrashAt, SimulatedCrash
from .conftest import drive, new_render, render_state
from .test_render_store import _job


async def _alerts(sessions: Sessions, t: Tenant, kind: str) -> list[str]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(text("SELECT message FROM alerts WHERE kind = :k"), {"k": kind})
            ).scalars()
        )


# ------------------------------------------------------------------ stuck sealing
@pytest.mark.parametrize("trigger", ["attempts", "elapsed"])
async def test_stuck_sealing_is_flagged_once_and_cleared_by_the_seal(
    app_sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    rs = settings.model_copy(
        update={"render_seal_stuck_attempts": 3, "render_seal_stuck_seconds": 3600}
        if trigger == "attempts"
        else {"render_seal_stuck_attempts": 1000, "render_seal_stuck_seconds": 0}
    )
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    run = RenderRun(app_sessions, s3, rs)
    assert await run.begin(t.tenant_id, render_id) == "rendering"
    assert await run.render_files(t.tenant_id, render_id) == "rendered"

    real = renders.anchor_if_due

    async def worm_down(*args: Any, force: bool = False, **kwargs: Any) -> str | None:
        if force:  # the seal's forced anchor cannot be written
            raise ConnectionError("WORM bucket unavailable")
        return await real(*args, force=force, **kwargs)

    monkeypatch.setattr(renders, "anchor_if_due", worm_down)
    for attempt in range(1, 5):
        with pytest.raises(ConnectionError):
            await run.complete(t.tenant_id, render_id)
        row = (await render_state(app_sessions, t.tenant_id, render_id))["row"]
        assert (row.status, row.seal_failures) == ("completed", attempt)
        stuck = attempt >= 3 if trigger == "attempts" else True
        assert (row.sealing_stuck_at is not None) is stuck, attempt
        assert _out(row).sealing_stuck is stuck
    assert "WORM bucket unavailable" in row.last_seal_error
    alerts = await _alerts(app_sessions, t, "render_sealing_stuck")
    assert len(alerts) == 1 and str(render_id) in alerts[0]  # flagged once, not per attempt

    monkeypatch.setattr(renders, "anchor_if_due", real)
    result = await run.complete(t.tenant_id, render_id)
    assert (result["status"], result["sealing_stuck"]) == ("completed", False)
    st = await render_state(app_sessions, t.tenant_id, render_id)
    out = _out(st["row"])
    assert out.sealed and not out.sealing_stuck and out.sealing_stuck_since is not None  # history
    assert st["audits"] == ["audit.render_completed"]
    report = await verify_chain(
        app_sessions, s3, rs, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert report.ok, report.errors


# ------------------------------------------------------------------ the sweeper
async def test_the_anchor_sweeper_anchors_an_abandoned_render_tail(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """A worker died after committing file batches that were not due for an anchor: the periodic
    sweeper anchors the render stream's tail, under the matter's retention, with the render id."""
    rs = settings.model_copy(
        update={"render_files_batch_size": 1, "custody_anchor_every_n_batches": 1000}
    )
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    with pytest.raises(SimulatedCrash):
        await drive(
            RenderRun(app_sessions, s3, rs, CrashAt("after_batch", 2)), t.tenant_id, render_id
        )
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        head = (
            await s.execute(
                text(
                    "SELECT last_seq, last_anchored_seq FROM custody_chain_heads WHERE stream_id = :r"
                ),
                {"r": render_id},
            )
        ).one()
    assert head.last_anchored_seq < head.last_seq  # an unanchored tail (the two batches)

    result = await sweep_anchors(
        sweeper_sessions, app_sessions, s3, rs, idle=timedelta(0), tenant_id=t.tenant_id
    )
    key = anchor_key(str(t.tenant_id), str(render_id), head.last_seq)
    assert key in result.anchored
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        anchor = (
            await s.execute(
                text(
                    "SELECT job_id, render_id, retain_until, state FROM evidence_objects"
                    " WHERE storage_key = :k"
                ),
                {"k": key},
            )
        ).one()
    assert (anchor.job_id, anchor.render_id, anchor.state) == (None, render_id, "complete")
    retain = effective_retain_until(rs, t.retention)
    assert abs((ensure_utc(anchor.retain_until) - retain).total_seconds()) < 120
    report = await verify_chain(
        app_sessions, s3, rs, tenant_id=t.tenant_id, stream_id=render_id, require_seal=False
    )
    assert report.ok and report.anchors_checked >= 2, report.errors


# ------------------------------------------------------------------ version routing
OTHER = {"renderer_version": "0.0.0-mixed", "unicode_version": "0.0.0", "tzdata_version": "1970a"}


class _Seen(CrashHooks):
    """Records which render workflows this worker ran activities for."""

    def __init__(self) -> None:
        self.workflows: set[str] = set()

    async def hit(self, point: str) -> None:
        self.workflows.add(activity.info().workflow_id)


async def test_a_mixed_version_pool_routes_every_render_to_matching_workers(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client
) -> None:
    """Two worker pools with different renderer/Unicode/tzdata versions run side by side. Renders
    recorded for either set of versions complete on the matching pool only; none fails from skew.
    (The second pool's versions are a test fiction: it runs this code under other version labels.)"""
    current = runtime_versions()
    seen = {"current": _Seen(), "other": _Seen()}
    pools = {
        "current": RenderActivities(app_sessions, s3, settings, seen["current"], current),
        "other": RenderActivities(app_sessions, s3, settings, seen["other"], OTHER),
    }
    t = await new_tenant(app_sessions)
    jobs = [await _job(app_sessions, s3, settings, t, epoch=0) for _ in range(2)]
    wanted: dict[str, str] = {}
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        for job_id in jobs:
            for pool, versions in (("current", current), ("other", OTHER)):
                made = await create_render(
                    s, tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
                    options=renders.RenderOptions(), requested_by="tests", versions=versions,
                )  # fmt: skip
                assert made.created
                wanted[str(made.render_id)] = pool
    async with (
        Worker(temporal, task_queue=pools["current"].task_queue, workflows=[RenderWorkflow],
               activities=pools["current"].all()),
        Worker(temporal, task_queue=pools["other"].task_queue, workflows=[RenderWorkflow],
               activities=pools["other"].all()),
    ):  # fmt: skip
        handles = []
        for render_id, pool in wanted.items():
            versions = current if pool == "current" else OTHER
            await start_render_workflow(
                temporal, settings, t.tenant_id, uuid.UUID(render_id), versions
            )
            handles.append(temporal.get_workflow_handle(f"render-{render_id}"))
        async with asyncio.timeout(90):
            results = await asyncio.gather(*(h.result() for h in handles))
    assert [r["status"] for r in results] == ["completed"] * len(wanted)
    for render_id, pool in wanted.items():
        st = await render_state(app_sessions, t.tenant_id, uuid.UUID(render_id))
        assert "render_failed" not in st["types"]
        started = st["events"][0].payload
        assert {k: started[k] for k in current} == (current if pool == "current" else OTHER)
    for pool in ("current", "other"):
        assert seen[pool].workflows == {f"render-{r}" for r, p in wanted.items() if p == pool}


async def test_a_misrouted_render_fails_instead_of_rendering_other_bytes(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """The safety net behind routing: a worker of other versions refuses to render."""
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        made = await create_render(
            s, tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
            options=renders.RenderOptions(), requested_by="tests", versions=OTHER,
        )  # fmt: skip
    run = RenderRun(app_sessions, s3, settings)  # this runtime's versions, not OTHER
    with pytest.raises(RenderIntegrityError, match="was requested for"):
        await run.begin(t.tenant_id, made.render_id)
    result = await run.fail(t.tenant_id, made.render_id, "RenderIntegrityError", "misrouted")
    assert result["status"] == "failed"
    st = await render_state(app_sessions, t.tenant_id, made.render_id)
    assert st["types"] == ["render_failed"] and st["productions"] == {}
