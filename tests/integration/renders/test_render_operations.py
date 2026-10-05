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

from edisc_api.routes.renders import RenderOut, render_out
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


async def _api(sessions: Sessions, t: Tenant, render_id: uuid.UUID) -> RenderOut:
    """The render as the API presents it (state and episodes)."""
    async with tenant_tx(sessions, t.tenant_id) as s:
        row = (await s.execute(text("SELECT * FROM renders WHERE id = :r"), {"r": render_id})).one()
        return await render_out(s, row)


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
        out = await _api(app_sessions, t, render_id)
        assert out.sealing_stuck is stuck, attempt
        assert out.state == ("sealing_stuck" if stuck else "completed")
    assert "WORM bucket unavailable" in row.last_seal_error
    alerts = await _alerts(app_sessions, t, "render_sealing_stuck")
    assert len(alerts) == 1 and str(render_id) in alerts[0]  # flagged once, not per attempt

    monkeypatch.setattr(renders, "anchor_if_due", real)
    result = await run.complete(t.tenant_id, render_id)
    assert (result["status"], result["sealing_stuck"]) == ("completed", False)
    st = await render_state(app_sessions, t.tenant_id, render_id)
    out = await _api(app_sessions, t, render_id)
    assert out.sealed and not out.sealing_stuck and out.sealing_stuck_since is None
    assert out.state == "completed"
    (episode,) = out.episodes  # the episode stays, closed by the seal
    assert (episode.kind, episode.end_reason) == ("sealing_stuck", "sealed")
    assert st["audits"] == ["audit.render_completed"]
    report = await verify_chain(
        app_sessions, s3, rs, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert report.ok, report.errors


async def test_a_sealed_render_cannot_get_stuck_again(
    app_sessions: Sessions, s3: S3Client, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stuck, sealed, then the WORM bucket fails again: a seal is final and recorded once, so later
    seal attempts are no-ops. No new episode, no new alert; the closed episode stays as history."""
    rs = settings.model_copy(update={"render_seal_stuck_attempts": 1})
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    run = RenderRun(app_sessions, s3, rs)
    await run.begin(t.tenant_id, render_id)
    await run.render_files(t.tenant_id, render_id)
    real = renders.anchor_if_due

    async def worm_down(*args: Any, force: bool = False, **kwargs: Any) -> str | None:
        if force:
            raise ConnectionError("WORM bucket unavailable")
        return await real(*args, force=force, **kwargs)

    monkeypatch.setattr(renders, "anchor_if_due", worm_down)
    with pytest.raises(ConnectionError):
        await run.complete(t.tenant_id, render_id)  # stuck (threshold 1)
    assert (await _api(app_sessions, t, render_id)).sealing_stuck
    monkeypatch.setattr(renders, "anchor_if_due", real)
    await run.complete(t.tenant_id, render_id)  # sealed
    sealed = await _api(app_sessions, t, render_id)
    monkeypatch.setattr(renders, "anchor_if_due", worm_down)
    for _ in range(3):  # a retried or duplicate completion after the seal
        result = await run.complete(t.tenant_id, render_id)
        assert (result["status"], result["sealing_stuck"]) == ("completed", False)
    again = await _api(app_sessions, t, render_id)
    assert again.sealed and not again.sealing_stuck and again.state == "completed"
    assert again.seal_storage_key == sealed.seal_storage_key and again.head_seq == sealed.head_seq
    assert [(e.kind, e.end_reason) for e in again.episodes] == [("sealing_stuck", "sealed")]
    assert len(await _alerts(app_sessions, t, "render_sealing_stuck")) == 1


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


# ------------------------------------------------------------------ unroutable renders
def _unserved() -> dict[str, str]:
    """A triple no worker serves (unique per test, so no other test's worker polls its queue)."""
    tag = uuid.uuid4().hex[:8]
    return {
        "renderer_version": f"0.0.0-{tag}",
        "unicode_version": "0.0.0",
        "tzdata_version": "1970a",
    }


async def _requested(
    sessions: Sessions, s3: S3Client, settings: Settings, versions: dict[str, str]
) -> tuple[Tenant, uuid.UUID]:
    t = await new_tenant(sessions)
    job_id = await _job(sessions, s3, settings, t, epoch=0)
    async with tenant_tx(sessions, t.tenant_id) as s:
        made = await create_render(
            s, tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
            options=renders.RenderOptions(), requested_by="tests", versions=versions,
        )  # fmt: skip
    return t, made.render_id


async def test_a_render_no_worker_serves_is_flagged_unroutable_once(
    app_sessions: Sessions,
    sweeper_sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    temporal: Client,
) -> None:
    from edisc_worker.render_routing import check_render_routing

    rs = settings.model_copy(update={"render_unroutable_seconds": 0})
    versions = _unserved()
    t, render_id = await _requested(app_sessions, s3, rs, versions)
    await start_render_workflow(temporal, rs, t.tenant_id, render_id, versions)

    for _ in range(2):  # the check runs every minute: one episode, one alert
        await check_render_routing(
            sweeper_sessions, app_sessions, temporal, rs, tenant_id=t.tenant_id
        )
    out = await _api(app_sessions, t, render_id)
    assert (out.status, out.state, out.unroutable) == ("requested", "unroutable", True)
    (episode,) = out.episodes
    assert episode.kind == "unroutable" and episode.ended_at is None
    assert renders.render_task_queue(**versions) in (episode.detail or "")
    alerts = await _alerts(app_sessions, t, "render_unroutable")
    assert len(alerts) == 1 and str(render_id) in alerts[0]

    # a worker of that triple appears: the render is picked up and the episode ends
    acts = RenderActivities(app_sessions, s3, rs, versions=versions)
    async with Worker(temporal, task_queue=acts.task_queue, workflows=[RenderWorkflow],
                      activities=acts.all()):  # fmt: skip
        async with asyncio.timeout(90):
            result = await temporal.get_workflow_handle(f"render-{render_id}").result()
    assert result["status"] == "completed"
    done = await _api(app_sessions, t, render_id)
    assert (done.state, done.unroutable) == ("completed", False)
    assert [(e.kind, e.end_reason) for e in done.episodes] == [("unroutable", "picked_up")]


async def test_losing_the_workers_again_opens_a_new_episode(
    app_sessions: Sessions,
    sweeper_sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    temporal: Client,
) -> None:
    """No worker (episode 1), a worker polls (episode 1 ends), the workers go away (episode 2, a new
    alert). The render's workflow is not started here, so the worker does not pick it up."""
    from edisc_worker.render_routing import check_render_routing

    rs = settings.model_copy(
        update={"render_unroutable_seconds": 0, "render_poller_max_age_seconds": 3}
    )
    versions = _unserved()
    t, render_id = await _requested(app_sessions, s3, rs, versions)

    async def check() -> None:
        await check_render_routing(
            sweeper_sessions, app_sessions, temporal, rs, tenant_id=t.tenant_id
        )

    await check()
    acts = RenderActivities(app_sessions, s3, rs, versions=versions)
    async with Worker(temporal, task_queue=acts.task_queue, workflows=[RenderWorkflow],
                      activities=acts.all()):  # fmt: skip
        await asyncio.sleep(0.5)  # its first poll
        await check()
    await asyncio.sleep(4)  # older than the poller max age
    await check()
    out = await _api(app_sessions, t, render_id)
    assert [(e.kind, e.end_reason) for e in out.episodes] == [
        ("unroutable", "worker_available"),
        ("unroutable", None),
    ]
    assert out.state == "unroutable" and out.unroutable_since == out.episodes[1].started_at
    assert len(await _alerts(app_sessions, t, "render_unroutable")) == 2
