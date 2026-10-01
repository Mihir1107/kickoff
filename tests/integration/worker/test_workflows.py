"""CollectionJobWorkflow / CollectUnitWorkflow end to end on the real Temporal server (ADR 0012)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from edisc_connector_dummy.dataset import Dataset
from edisc_connectors_base.types import Connection
from edisc_core.schemas import JobStatus
from edisc_custody.log import append, verify_chain
from edisc_db.session import sqlstate_of, tenant_tx
from edisc_evidence.worm import WormConflictError
from edisc_worker.pipeline import CollectOutcome, CrashHooks, Pipeline

from ..pipeline.conftest import JobRun, assert_invariants
from .conftest import (
    Harness,
    activity_attempts,
    fast,
    queue,
    replace_cfg,
    replay_and_record,
    spec,
)

pytestmark = pytest.mark.timeout(120)


async def _units(h: Harness, t: Any, job_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_tx(h.sessions, t.tenant_id) as s:
        rows = (
            await s.execute(
                text("SELECT * FROM work_units WHERE job_id = :j ORDER BY unit_key"), {"j": job_id}
            )
        ).all()
    return {r.unit_key: r for r in rows}


async def test_job_matches_the_oracle_through_continue_as_new(harness: Harness) -> None:
    sp = spec()
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    q = queue()
    async with harness.worker(q, settings=fast(harness.settings)):
        handle = await harness.start(t, job_id, q)
        status = await handle.result()
    assert status == JobStatus.COMPLETED.value
    job = await harness.job(t, job_id)
    assert job.sealed_at is not None
    await assert_invariants(
        harness.sessions,
        harness.s3,
        harness.settings,
        t,
        sp,
        JobRun(job_id, JobStatus(status), False),
        0,
    )
    histories = await replay_and_record(harness.temporal, job_id, "job-clean")
    runs: dict[str, int] = {}
    for h in histories:
        runs[h.workflow_id] = runs.get(h.workflow_id, 0) + 1
    assert runs[str(job_id)] > 1, "the parent never continued-as-new"
    assert any(n > 1 for wid, n in runs.items() if "/" in wid), "no child continued-as-new"


async def test_injected_source_failures_still_give_exact_results(harness: Harness) -> None:
    sp = spec(
        failures={"seed": 5, "exception_rate": 0.25, "timeout_rate": 0.15, "max_consecutive": 2}
    )
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    q = queue()
    async with harness.worker(q, settings=fast(harness.settings)) as acts:
        status = await (await harness.start(t, job_id, q)).result()
    assert status == JobStatus.COMPLETED.value
    injected = acts.connectors["dummy"]._attempts  # type: ignore[attr-defined]
    assert sum(injected.values()) >= 5, "failure injection did not fire: the test proves nothing"
    await assert_invariants(
        harness.sessions,
        harness.s3,
        harness.settings,
        t,
        sp,
        JobRun(job_id, JobStatus(status), False),
        0,
    )
    await replay_and_record(harness.temporal, job_id, "job-retries")


async def test_unit_integrity_failure_fails_only_that_unit_and_reruns_as_a_new_job(
    harness: Harness,
) -> None:
    broken = spec(failures={"corrupt_conversations": [1]})
    t = await harness.tenant(broken)
    job_id = await harness.create_job(t, broken)
    q = queue()
    async with harness.worker(q, settings=fast(harness.settings)):
        status = await (await harness.start(t, job_id, q)).result()
        assert status == JobStatus.COMPLETED_WITH_FAILED_UNITS.value
        units = await _units(harness, t, job_id)
        failed = {k for k, u in units.items() if u.status == "failed"}
        assert failed and all(units[k].last_error.startswith("InvalidCursorError") for k in failed)
        assert all(u.status == "done" for k, u in units.items() if k not in failed)
        events = await harness.custody_types(t, job_id)
        assert events.count("unit_failed") == len(failed)
        assert events[-1] == "job_finished"
        report = await verify_chain(
            harness.sessions, harness.s3, harness.settings, tenant_id=t.tenant_id, stream_id=job_id
        )
        assert report.ok, report.errors

        # fixed: failed units re-run as a NEW job; the sealed original stays closed
        fixed = spec()
        await harness.set_config(t, fixed)
        rerun = await Pipeline(
            harness.sessions, harness.s3, harness.settings, harness.activities().connectors["dummy"]
        ).create_rerun_job(tenant_id=t.tenant_id, original_job_id=job_id, requested_by="tester")
        rerun_status = await (await harness.start(t, rerun, q)).result()
    assert rerun_status == JobStatus.COMPLETED.value
    assert set(await _units(harness, t, rerun)) == failed
    assert (await harness.job(t, rerun)).rerun_of == job_id
    assert (await harness.job(t, job_id)).status == JobStatus.COMPLETED_WITH_FAILED_UNITS.value
    # the original job plus its rerun hold exactly the oracle's data
    await assert_invariants(
        harness.sessions,
        harness.s3,
        harness.settings,
        t,
        fixed,
        JobRun(rerun, JobStatus.COMPLETED, False),
        0,
    )


class AnchorConflictAt(CrashHooks):
    """A job-scoped integrity failure (WORM anchor conflict) at the nth committed batch."""

    def __init__(self, nth: int) -> None:
        self.nth, self.seen = nth, 0

    async def hit(self, point: str) -> None:
        if point == "after_commit":
            self.seen += 1
            if self.seen == self.nth:
                raise WormConflictError("anchor object exists with different content (injected)")


async def test_job_integrity_failure_fails_the_job_and_nothing_lands_after_the_seal(
    harness: Harness,
) -> None:
    sp = spec(conversations=4, messages_per_unit=24)
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    q = queue()
    async with harness.worker(q, settings=fast(harness.settings), hooks=AnchorConflictAt(4)):
        status = await (await harness.start(t, job_id, q)).result()
    assert status == JobStatus.FAILED.value
    job = await harness.job(t, job_id)
    assert job.stop_reason == "job_failure" and job.sealed_at is not None
    units = await _units(harness, t, job_id)
    assert any(u.status not in ("done", "failed") for u in units.values()), (
        "other units must have been stopped mid-run"
    )
    events = await harness.custody_types(t, job_id)
    assert "job_failed" in events and events[-1] == "job_finished"
    report = await verify_chain(
        harness.sessions, harness.s3, harness.settings, tenant_id=t.tenant_id, stream_id=job_id
    )
    assert report.ok, report.errors

    # a straggler (zombie activity) can neither collect nor append once the job is sealed
    p = Pipeline(
        harness.sessions, harness.s3, harness.settings, harness.activities().connectors["dummy"]
    )
    open_unit = next(k for k, u in units.items() if u.status not in ("done", "failed"))
    conn = Connection(
        t.tenant_id,
        t.connection_id,
        "dummy",
        sp.workspace_id,
        {"spec": sp.model_dump(mode="json"), "epoch": 0},
    )
    assert (
        await p.collect_pages(tenant_id=t.tenant_id, job_id=job_id, unit_key=open_unit, conn=conn)
        is CollectOutcome.STOPPED
    )
    with pytest.raises(DBAPIError) as closed:
        async with tenant_tx(harness.sessions, t.tenant_id) as s:
            await append(
                s,
                tenant_id=t.tenant_id,
                stream_id=job_id,
                job_id=job_id,
                event_type="items_collected",
                actor="zombie",
                payload={"unit_key": open_unit},
            )
    assert sqlstate_of(closed.value) == "EA005"  # guard_job_open
    assert await harness.custody_types(t, job_id) == events


async def test_revoked_credentials_pause_every_job_on_the_connection_until_reauth(
    harness: Harness,
) -> None:
    sp = spec()
    t = await harness.tenant(sp, auth_revoked=True)
    first, second = await harness.create_job(t, sp), await harness.create_job(t, sp)
    q = queue()
    async with harness.worker(q, settings=fast(harness.settings)):
        h1 = await harness.start(t, first, q)
        h2 = await harness.start(t, second, q)
        for _ in range(100):
            jobs = [await harness.job(t, j) for j in (first, second)]
            if all(j.status == "paused_awaiting_reauth" for j in jobs):
                break
            await asyncio.sleep(0.2)
        assert [j.status for j in jobs] == ["paused_awaiting_reauth"] * 2
        async with tenant_tx(harness.sessions, t.tenant_id) as s:
            alerts = (
                await s.execute(
                    text("SELECT kind, connection_id FROM alerts WHERE connection_id = :c"),
                    {"c": t.connection_id},
                )
            ).all()
            pauses = (
                (
                    await s.execute(
                        text(
                            "SELECT job_id FROM job_pauses WHERE connection_id = :c AND resumed_at IS NULL"
                        ),
                        {"c": t.connection_id},
                    )
                )
                .scalars()
                .all()
            )
            conn_status = (
                await s.execute(
                    text("SELECT status FROM connections WHERE id = :c"), {"c": t.connection_id}
                )
            ).scalar_one()
        assert [a.kind for a in alerts] == ["reauth_required"]  # one alert per connection
        assert set(pauses) == {first, second}
        assert conn_status == "reauth_required"
        await asyncio.sleep(1.0)  # paused: nothing runs, nothing is retried in a loop
        assert [(await harness.job(t, j)).status for j in (first, second)] == [
            "paused_awaiting_reauth"
        ] * 2

        # re-authorized (M13 API): credentials fixed, pauses closed, workflows woken
        await harness.set_config(t, sp)
        await Pipeline(
            harness.sessions, harness.s3, harness.settings, harness.activities().connectors["dummy"]
        ).resume_connection(tenant_id=t.tenant_id, connection_id=t.connection_id, actor="tester")
        await h1.signal("wake")
        await h2.signal("wake")
        results = [await h1.result(), await h2.result()]
    assert results == [JobStatus.COMPLETED.value] * 2
    for j in (first, second):
        job = await harness.job(t, j)
        assert job.status_detail["paused_ms"] > 0
        events = await harness.custody_types(t, j)
        assert events.index("job_paused") < events.index("job_resumed")
    await assert_invariants(
        harness.sessions,
        harness.s3,
        harness.settings,
        t,
        sp,
        JobRun(second, JobStatus.COMPLETED, False),
        0,
    )
    await replay_and_record(harness.temporal, first, "job-auth-pause")


async def test_transient_exhaustion_defers_units_then_fails_them_after_the_horizon(
    harness: Harness,
) -> None:
    sp = spec(failures={"unavailable_conversations": [0]})
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    q = queue()
    settings = fast(harness.settings, unit_retry_cooldown_seconds=0.5, unit_retry_horizon_seconds=3)
    async with harness.worker(q, settings=settings):
        status = await (await harness.start(t, job_id, q, replace_cfg(max_attempts=2))).result()
    assert status == JobStatus.COMPLETED_WITH_FAILED_UNITS.value
    units = await _units(harness, t, job_id)
    failed = {k: u for k, u in units.items() if u.status == "failed"}
    broken_conv = Dataset(sp).conversations()[0].id
    assert {u.conversation_id for u in failed.values()} == {broken_conv}
    for u in failed.values():
        assert u.failures >= 2  # deferred (retry_later) at least once before the horizon
        assert "TransientExhausted" in u.last_error and "DummySourceError" in u.last_error
    assert all(u.status == "done" for k, u in units.items() if k not in failed)


@pytest.mark.parametrize("how", ["signal", "temporal_cancel"])
async def test_cancel_stops_at_a_batch_boundary_and_seals(harness: Harness, how: str) -> None:
    sp = spec(conversations=4, messages_per_unit=30)
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    q = queue()
    async with harness.worker(q, settings=fast(harness.settings)):
        handle = await harness.start(t, job_id, q, replace_cfg(max_units_in_flight=1))
        for _ in range(200):
            async with tenant_tx(harness.sessions, t.tenant_id) as s:
                batches = (
                    await s.execute(
                        text(
                            "SELECT count(*) FROM custody_events WHERE stream_id = :j AND event_type = 'items_collected'"
                        ),
                        {"j": job_id},
                    )
                ).scalar_one()
            if batches >= 2:
                break
            await asyncio.sleep(0.05)
        if how == "signal":
            await handle.signal("cancel")
        else:
            await handle.cancel()
        status = await handle.result()
    assert status == JobStatus.CANCELLED.value
    job = await harness.job(t, job_id)
    assert job.sealed_at is not None and job.stop_reason == "cancel"
    events = await harness.custody_types(t, job_id)
    assert "cancel_requested" in events and events[-1] == "job_cancelled"
    units = await _units(harness, t, job_id)
    assert any(u.status != "done" for u in units.values()), "cancel came too late to test anything"
    report = await verify_chain(
        harness.sessions, harness.s3, harness.settings, tenant_id=t.tenant_id, stream_id=job_id
    )
    assert report.ok, report.errors
    if how == "signal":
        await replay_and_record(harness.temporal, job_id, "job-cancel")


class Boom(CrashHooks):
    async def hit(self, point: str) -> None:
        if point == "after_commit":
            raise RuntimeError("unexpected bug")


async def test_unclassified_errors_fail_the_unit_after_a_bounded_number_of_attempts(
    harness: Harness,
) -> None:
    sp = spec(conversations=1, days=1)
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    q = queue()
    async with harness.worker(q, settings=fast(harness.settings), hooks=Boom()):
        status = await (await harness.start(t, job_id, q)).result()
    assert status == JobStatus.COMPLETED_WITH_FAILED_UNITS.value
    units = await _units(harness, t, job_id)
    conv = [u for u in units.values() if u.kind == "conversation_day"]
    assert conv and all(
        u.status == "failed" and u.last_error.startswith("RuntimeError") for u in conv
    )
    # each attempt committed at most one batch before the bug; the record stays consistent
    report = await verify_chain(
        harness.sessions, harness.s3, harness.settings, tenant_id=t.tenant_id, stream_id=job_id
    )
    assert report.ok, report.errors


@pytest.mark.parametrize(
    ("time_box", "cfg"),
    [
        # long Retry-After waits heartbeat: a 3 s pause with a 1.5 s heartbeat timeout never times out
        (30.0, {"heartbeat_timeout_seconds": 1.5}),
        # the time box also ends an activity DURING a wait: never exceeds start-to-close
        (0.5, {"start_to_close_seconds": 2.5}),
    ],
    ids=["heartbeat_during_wait", "time_box_during_wait"],
)
async def test_source_throttling_never_times_out_an_activity(
    harness: Harness, time_box: float, cfg: dict[str, float]
) -> None:
    sp = spec(
        conversations=2,
        days=1,
        failures={"seed": 3, "throttle_rate": 0.15, "max_consecutive": 1, "retry_after_seconds": 3},
    )
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    q = queue()
    async with harness.worker(
        q, settings=fast(harness.settings, activity_time_box_seconds=time_box)
    ) as acts:
        status = await (await harness.start(t, job_id, q, replace_cfg(**cfg))).result()
    assert status == JobStatus.COMPLETED.value
    assert sum(acts.connectors["dummy"]._attempts.values()) >= 1, "no 429 was injected"  # type: ignore[attr-defined]
    histories = await replay_and_record(
        harness.temporal,
        job_id,
    )
    attempts = activity_attempts(histories)
    assert attempts and max(attempts) == 1, f"an activity was retried: {attempts}"
    await assert_invariants(
        harness.sessions,
        harness.s3,
        harness.settings,
        t,
        sp,
        JobRun(job_id, JobStatus.COMPLETED, False),
        0,
    )
