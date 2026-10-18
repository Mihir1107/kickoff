"""A job paused for re-authorization ends with its access lost, never open-ended (M17 step 8; ADR
0016 A6, A2.1, A2.2; plan §2.6, §8): the pause records its reason class, deadline and warning time
and alerts once; the warning alert comes once; past the deadline every unit not yet reconciled
closes ``access_lost`` with the reason class, each with its chain event, the job finalizes
``completed_with_gaps`` and its report's units recompute from the job package byte for byte; a
crash while closing resumes and closes the rest, the stop reason only after the last unit; a
re-authorization before the deadline resumes with zero gaps and closes the alerts; a job that lost
access before it enumerated anything is never clean. ``check-connections`` turns a rejected
connection ``reauth_required`` before a job finds out."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from edisc_connector_dummy.connector import DummyConnector
from edisc_core.schemas import JobStatus
from edisc_custody.export import export_package
from edisc_db.session import tenant_tx
from edisc_renderers.report.recompute import recompute
from edisc_worker import access_lost
from edisc_worker.pipeline import CrashHooks, Pipeline
from edisc_worker.report_loader import ReportLoader

from ..normalizer.harness import Tenant
from .conftest import Harness, fast, queue, spec

DEADLINE = 6  # seconds (EDISC_REAUTH_DEADLINE_OVERRIDE_SECONDS: test/ci only); warning at half


class RevokeAfterFirstBatch(CrashHooks):
    """The connection's credentials are revoked once the first batch committed: later source calls
    are refused, so the job pauses mid-way with some units reconciled and others not."""

    def __init__(self, harness: Harness, t: Tenant, sp: Any) -> None:
        self.harness, self.t, self.sp, self.done = harness, t, sp, False

    async def hit(self, point: str) -> None:
        if point == "after_commit" and not self.done:
            self.done = True
            await self.harness.set_config(self.t, self.sp, auth_revoked=True)


async def _rows(h: Harness, t: Tenant, sql: str, **params: Any) -> list[Any]:
    async with tenant_tx(h.sessions, t.tenant_id) as s:
        return list((await s.execute(text(sql), params)).all())


async def _until(h: Harness, t: Tenant, sql: str, **params: Any) -> None:
    """Wait for a condition the DATABASE states (its clock, its rows): never a fixed sleep."""
    for _ in range(600):
        if (await _rows(h, t, sql, **params))[0][0]:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never true: {sql}")


def _pipeline(h: Harness, settings: Any) -> Pipeline:
    return Pipeline(h.sessions, h.s3, settings, DummyConnector(h.limiter))


async def _paused_job(
    h: Harness, settings: Any, *, mid_way: bool = True
) -> tuple[Tenant, Any, uuid.UUID, Any]:
    sp = spec(conversations=4)
    t = await h.tenant(sp, auth_revoked=not mid_way)
    job_id = await h.create_job(t, sp)
    return t, sp, job_id, RevokeAfterFirstBatch(h, t, sp) if mid_way else CrashHooks()


async def test_past_its_deadline_a_paused_job_closes_every_remaining_unit_access_lost(
    harness: Harness, sweeper_sessions: Any, tmp_path: Path
) -> None:
    h = harness
    settings = fast(h.settings, reauth_deadline_override_seconds=DEADLINE)
    t, sp, job_id, hooks = await _paused_job(h, settings)
    async with h.worker(q := queue(), settings=settings, hooks=hooks):
        handle = await h.start(t, job_id, q)
        await _until(h, t, "SELECT status = 'paused_awaiting_reauth' FROM collection_jobs"
                     " WHERE id = :j", j=job_id)  # fmt: skip
        (pause,) = await _rows(h, t, "SELECT * FROM job_pauses WHERE job_id = :j", j=job_id)
        assert pause.reason_class == "credentials_revoked"
        assert (pause.deadline_at - pause.paused_at).total_seconds() == DEADLINE
        assert (pause.warn_at - pause.paused_at).total_seconds() == DEADLINE / 2  # under 48 h
        kinds = [r.kind for r in await _rows(h, t, "SELECT kind FROM alerts WHERE job_id = :j",
                                             j=job_id)]  # fmt: skip
        assert kinds == ["job_paused"]  # one alert at the pause, naming the job
        pipe = _pipeline(h, settings)
        sweep = await access_lost.check_reauth_deadlines(sweeper_sessions, pipe, h.temporal,
                                                         tenant_id=t.tenant_id)  # fmt: skip
        assert (sweep.warned, sweep.closed_jobs) == (0, 0)  # nothing due yet
        await _until(h, t, "SELECT now() >= warn_at FROM job_pauses WHERE job_id = :j", j=job_id)
        # the warning comes once, even from two sweeps at the same moment (each locks the pause
        # and re-reads whether it was warned), and never again after
        racing = await asyncio.gather(*(
            access_lost.check_reauth_deadlines(sweeper_sessions, pipe, h.temporal,
                                               tenant_id=t.tenant_id) for _ in range(2)))  # fmt: skip
        assert sorted(r.warned for r in racing) == [0, 1] and not any(r.closed_jobs for r in racing)
        sweep = await access_lost.check_reauth_deadlines(sweeper_sessions, pipe, h.temporal,
                                                         tenant_id=t.tenant_id)  # fmt: skip
        assert (sweep.warned, sweep.closed_jobs) == (0, 0)
        warnings = await _rows(h, t, "SELECT count(*) FROM alerts WHERE job_id = :j"
                               " AND kind = 'reauth_deadline_near'", j=job_id)  # fmt: skip
        assert warnings == [(1,)]
        await _until(h, t, "SELECT now() >= deadline_at FROM job_pauses WHERE job_id = :j",
                     j=job_id)  # fmt: skip
        open_before = await _rows(h, t, "SELECT unit_key FROM work_units WHERE job_id = :j"
                                  " AND status NOT IN ('done', 'failed')", j=job_id)  # fmt: skip
        assert open_before, "the revocation came after every unit: nothing to prove"
        sweep = await access_lost.check_reauth_deadlines(sweeper_sessions, pipe, h.temporal,
                                                         tenant_id=t.tenant_id)  # fmt: skip
        assert (sweep.closed_jobs, sweep.units_closed) == (1, len(open_before))
        status = await handle.result()
    assert status == JobStatus.COMPLETED_WITH_GAPS.value
    units = await _rows(h, t, "SELECT unit_key, status, recon_status, access_lost_reason"
                        " FROM work_units WHERE job_id = :j", j=job_id)  # fmt: skip
    closed = {u.unit_key for u in open_before}
    for u in units:
        assert u.status == "done"
        if u.unit_key in closed:
            assert (u.recon_status, u.access_lost_reason) == ("access_lost", "credentials_revoked")
    events = await _rows(h, t, "SELECT event_type, actor, payload FROM custody_events"
                         " WHERE stream_id = :j ORDER BY seq", j=job_id)  # fmt: skip
    kinds = [e.event_type for e in events]
    ended = kinds.index("job_pause_ended")
    assert kinds.index("job_paused") < ended < kinds.index("job_finished")
    after = [e for e in events[ended:] if e.event_type == "unit_reconciled"]
    assert {e.payload["unit_key"] for e in after} == closed  # each closed unit, in the chain
    assert all(e.payload["access_lost_reason"] == "credentials_revoked" for e in after)
    assert events[ended].payload["ended_how"] == "deadline"
    assert events[ended].actor == access_lost.DEADLINE_ACTOR
    (final,) = [e for e in events if e.event_type == "job_finished"]
    assert final.payload["stop_reason"] == "access_lost"
    (pause,) = await _rows(h, t, "SELECT * FROM job_pauses WHERE job_id = :j", j=job_id)
    assert pause.ended_how == "deadline" and pause.resumed_at is not None
    open_alerts = await _rows(h, t, "SELECT kind FROM alerts WHERE job_id = :j"
                              " AND resolved_at IS NULL", j=job_id)  # fmt: skip
    assert open_alerts == []  # closing the pause closed its alerts
    # the report states every unit from the chain, and units.jsonl recomputes from the package
    files: dict[str, bytes] = {}

    async def sink(name: str, chunks: Any) -> None:
        files[name] = b"".join([c async for c in chunks])

    built = await ReportLoader(h.sessions, h.s3, h.settings, tenant_id=t.tenant_id,
                               job_id=job_id).build(sink)  # fmt: skip
    assert not built.clean
    # how the pause ended, from the chain, and the database agreeing (no pauses_differ)
    (stated,) = built.document["pauses"]["pauses"]
    assert (stated["ended_how"], stated["ended_by"]) == ("deadline", access_lost.DEADLINE_ACTOR)
    assert stated["reason_class"] == "credentials_revoked" and stated["end_reason"] is None
    assert stated["deadline_at"] == events[kinds.index("job_paused")].payload["deadline_at"]
    assert stated["duration_ms"] > 0
    assert not [d for d in built.document["divergences"]["divergences"]
                if d["kind"] == "pauses_differ"]  # fmt: skip
    pkg = await export_package(h.sessions, h.s3, h.settings, tenant_id=t.tenant_id,
                               job_id=job_id, dest=tmp_path / "job", include_objects=False)  # fmt: skip
    chain = [json.loads(line) for line in (pkg / "events.jsonl").read_bytes().splitlines()]
    done = recompute(chain, built.report_json, files["units.jsonl"])
    assert done.ok, done.differences
    rows = [json.loads(line) for line in files["units.jsonl"].splitlines()]
    assert {
        r["unit_key"] for r in rows if r.get("access_lost_reason") == "credentials_revoked"
    } == closed


class StopClosingAfter(CrashHooks):
    def __init__(self, n: int) -> None:
        self.left = n

    async def hit(self, point: str) -> None:
        if point == "access_lost_unit_closed":
            self.left -= 1
            if self.left < 0:
                raise RuntimeError("crash point: while closing units access_lost")


async def test_a_crash_while_closing_resumes_and_the_stop_comes_after_the_last_unit(
    harness: Harness, sweeper_sessions: Any
) -> None:
    h = harness
    settings = fast(h.settings, reauth_deadline_override_seconds=DEADLINE)
    t, sp, job_id, hooks = await _paused_job(h, settings)
    async with h.worker(q := queue(), settings=settings, hooks=hooks):
        handle = await h.start(t, job_id, q)
        await _until(h, t, "SELECT status = 'paused_awaiting_reauth' FROM collection_jobs"
                     " WHERE id = :j", j=job_id)  # fmt: skip
        await _until(h, t, "SELECT now() >= deadline_at FROM job_pauses WHERE job_id = :j",
                     j=job_id)  # fmt: skip
        crashing = Pipeline(
            h.sessions, h.s3, settings, DummyConnector(h.limiter), StopClosingAfter(1)
        )
        with pytest.raises(RuntimeError, match="crash point"):
            await access_lost.check_reauth_deadlines(sweeper_sessions, crashing, h.temporal,
                                                     tenant_id=t.tenant_id)  # fmt: skip
        (job,) = await _rows(h, t, "SELECT stop_reason, status FROM collection_jobs WHERE id = :j",
                             j=job_id)  # fmt: skip
        assert (job.stop_reason, job.status) == (None, "paused_awaiting_reauth")  # not before all
        left = await _rows(h, t, "SELECT count(*) FROM work_units WHERE job_id = :j"
                           " AND status NOT IN ('done', 'failed')", j=job_id)  # fmt: skip
        assert left[0][0] > 0
        sweep = await access_lost.check_reauth_deadlines(sweeper_sessions, _pipeline(h, settings),
                                                         h.temporal, tenant_id=t.tenant_id)  # fmt: skip
        assert sweep.closed_jobs == 1 and sweep.units_closed == left[0][0]
        assert await handle.result() == JobStatus.COMPLETED_WITH_GAPS.value
    ended = [e for e in await _rows(h, t, "SELECT event_type FROM custody_events WHERE stream_id"
                                    " = :j", j=job_id) if e.event_type == "job_pause_ended"]  # fmt: skip
    assert len(ended) == 1  # decided once, carried out across two runs


async def test_reauthorization_before_the_deadline_resumes_with_zero_gaps(
    harness: Harness, sweeper_sessions: Any
) -> None:
    h = harness
    settings = fast(h.settings, reauth_deadline_override_seconds=3600)
    t, sp, job_id, hooks = await _paused_job(h, settings)
    async with h.worker(q := queue(), settings=settings, hooks=hooks):
        handle = await h.start(t, job_id, q)
        await _until(h, t, "SELECT status = 'paused_awaiting_reauth' FROM collection_jobs"
                     " WHERE id = :j", j=job_id)  # fmt: skip
        await h.set_config(t, sp)
        await _pipeline(h, settings).resume_connection(
            tenant_id=t.tenant_id, connection_id=t.connection_id, actor="user:dana"
        )
        await handle.signal("wake")
        assert await handle.result() == JobStatus.COMPLETED.value
    (pause,) = await _rows(h, t, "SELECT * FROM job_pauses WHERE job_id = :j", j=job_id)
    assert (pause.ended_how, pause.ended_by) == ("reauthorized", "user:dana")
    assert await _rows(h, t, "SELECT kind FROM alerts WHERE job_id = :j AND resolved_at IS NULL",
                       j=job_id) == []  # fmt: skip
    sweep = await access_lost.check_reauth_deadlines(sweeper_sessions, _pipeline(h, settings),
                                                     h.temporal, tenant_id=t.tenant_id)  # fmt: skip
    assert (sweep.closed_jobs, sweep.warned) == (0, 0)  # a resumed pause is never swept


async def test_access_lost_before_enumeration_is_never_clean(
    harness: Harness, sweeper_sessions: Any
) -> None:
    """Revoked from the start: the job never learned what to collect. Closing "every remaining
    unit" closes nothing, so the job must not finish clean: it fails, stating the stop reason."""
    h = harness
    settings = fast(h.settings, reauth_deadline_override_seconds=DEADLINE)
    t, sp, job_id, hooks = await _paused_job(h, settings, mid_way=False)
    async with h.worker(q := queue(), settings=settings, hooks=hooks):
        handle = await h.start(t, job_id, q)
        await _until(h, t, "SELECT status = 'paused_awaiting_reauth' FROM collection_jobs"
                     " WHERE id = :j", j=job_id)  # fmt: skip
        assert await _rows(h, t, "SELECT count(*) FROM work_units WHERE job_id = :j",
                           j=job_id) == [(0,)]  # fmt: skip
        await _until(h, t, "SELECT now() >= deadline_at FROM job_pauses WHERE job_id = :j",
                     j=job_id)  # fmt: skip
        await access_lost.check_reauth_deadlines(sweeper_sessions, _pipeline(h, settings),
                                                 h.temporal, tenant_id=t.tenant_id)  # fmt: skip
        assert await handle.result() == JobStatus.FAILED.value
    (job,) = await _rows(h, t, "SELECT stop_reason, status_detail FROM collection_jobs"
                         " WHERE id = :j", j=job_id)  # fmt: skip
    assert job.stop_reason == "access_lost" and job.status_detail["units_enumerated"] is False


async def test_check_connections_finds_a_revoked_connection_before_a_job_does(
    harness: Harness, sweeper_sessions: Any
) -> None:
    h = harness
    sp = spec()
    good, revoked = await h.tenant(sp), await h.tenant(sp, auth_revoked=True)
    for t in (good, revoked):  # credentials stored, as an install leaves them
        async with tenant_tx(h.sessions, t.tenant_id) as s:
            await s.execute(text("UPDATE connections SET token_version = 1 WHERE id = :c"),
                            {"c": t.connection_id})  # fmt: skip
    pipe = _pipeline(h, h.settings)
    connectors = {"dummy": DummyConnector(h.limiter)}
    for t, want in ((good, "active"), (revoked, "reauth_required")):
        done = await access_lost.check_connections(sweeper_sessions, pipe, connectors,
                                                   tenant_id=t.tenant_id)  # fmt: skip
        assert (done.ok, done.revoked) == ((1, 0) if want == "active" else (0, 1))
        (c,) = await _rows(h, t, "SELECT status, credentials_checked_at FROM connections"
                           " WHERE id = :c", c=t.connection_id)  # fmt: skip
        assert c.status == want and c.credentials_checked_at is not None  # attempted
    alerts = await _rows(h, revoked, "SELECT kind FROM alerts WHERE connection_id = :c",
                         c=revoked.connection_id)  # fmt: skip
    assert [a.kind for a in alerts] == ["reauth_required"]  # even with no job running
    # a source no connector of this worker serves is counted, never reported as fine
    done = await access_lost.check_connections(sweeper_sessions, pipe, {},
                                               tenant_id=good.tenant_id)  # fmt: skip
    assert (done.ok, done.unchecked) == (0, {"dummy": 1})


async def test_a_refusal_of_replaced_credentials_never_pauses_the_job_again(
    harness: Harness,
) -> None:
    """The race behind an intermittent hang of the re-authorization test: a unit activity loaded
    the revoked credentials, the connection was re-authorized and the job resumed, THEN that
    activity's refusal arrived and paused the resumed job again (for ever, in the test). A refusal
    carries the stamp of the credentials it used; it pauses only while they are still in force."""
    from temporalio.testing import ActivityEnvironment

    from edisc_worker.activities import credentials_stamp
    from edisc_worker.contracts import JobRef, PauseRequest

    h = harness
    sp = spec()
    t = await h.tenant(sp, auth_revoked=True)
    job_id = await h.create_job(t, sp)
    async with tenant_tx(h.sessions, t.tenant_id) as s:
        await s.execute(text("UPDATE collection_jobs SET status = 'running' WHERE id = :j"),
                        {"j": job_id})  # fmt: skip
        old = (await s.execute(text("SELECT token_version, config FROM connections WHERE id = :c"),
                               {"c": t.connection_id})).one()  # fmt: skip
    refused = credentials_stamp(old.token_version, old.config)
    reason = f"AuthenticationError: source rejected credentials (credentials_revoked): x [credentials {refused}]"
    acts = h.activities()
    await h.set_config(t, sp)  # re-authorized before the stale refusal is handled
    ref = JobRef(str(t.tenant_id), str(job_id))
    assert await ActivityEnvironment().run(acts.pause_for_reauth, PauseRequest(ref, reason)) == 0
    assert (await h.job(t, job_id)).status == "running"
    # the same refusal of the credentials in force pauses
    async with tenant_tx(h.sessions, t.tenant_id) as s:
        now = (await s.execute(text("SELECT token_version, config FROM connections WHERE id = :c"),
                               {"c": t.connection_id})).one()  # fmt: skip
    current = reason.replace(refused, credentials_stamp(now.token_version, now.config))
    assert await ActivityEnvironment().run(acts.pause_for_reauth, PauseRequest(ref, current)) == 1
    assert (await h.job(t, job_id)).status == "paused_awaiting_reauth"
