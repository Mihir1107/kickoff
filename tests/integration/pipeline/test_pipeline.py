"""M11: clean jobs vs the oracle, reconciliation, the crash matrix, failure modes."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.dataset import Dataset
from edisc_core.schemas import JobStatus
from edisc_core.settings import Settings

from ..normalizer.harness import new_tenant
from .conftest import CrashAt, assert_invariants, event_items, run_job, spec, units

Sessions = async_sessionmaker[AsyncSession]


@pytest.mark.parametrize("dialect", ["slack", "slack_history"])
async def test_clean_jobs_over_three_epochs_match_the_oracle(
    app_sessions: Sessions, s3: S3Client, settings: Settings, dialect: str
) -> None:
    sp = spec(dialect=dialect)
    ds = Dataset(sp)
    t = await new_tenant(app_sessions)
    for epoch in (0, 1, 2):
        run = await run_job(app_sessions, s3, settings, t, sp, epoch)
        assert run.status is JobStatus.COMPLETED
        for u in await units(app_sessions, t, run.job_id):
            if u.kind == "directory":
                assert u.recon_status == "not_applicable"
                continue
            truth = ds.expected_count(u.conversation_id, ds.day_index(u.day), epoch)
            assert (u.expected_count, u.collected_count, u.recon_status) == (
                truth,
                truth,
                "matched",
            )
        await assert_invariants(app_sessions, s3, settings, t, sp, run, epoch)


CRASH_POINTS = ["after_evidence", "mid_transaction", "after_commit", "during_finalize"]


# 1st and 5th occurrence: every boundary is reached at least 5 times per job (finalize: 6 units + directory + job)
@pytest.mark.parametrize("nth", [1, 5])
@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_crash_matrix_resumes_to_oracle_exact_results(
    app_sessions: Sessions, s3: S3Client, settings: Settings, point: str, nth: int
) -> None:
    sp = spec(dialect="slack_history")  # absence detection is exercised in epoch 1
    t = await new_tenant(app_sessions)
    first = await run_job(app_sessions, s3, settings, t, sp, 0, CrashAt(point, nth))
    assert first.crashed
    await assert_invariants(app_sessions, s3, settings, t, sp, first, 0)
    second = await run_job(app_sessions, s3, settings, t, sp, 1, CrashAt(point, nth))
    assert second.crashed
    assert second.status is JobStatus.COMPLETED
    await assert_invariants(app_sessions, s3, settings, t, sp, second, 1)


async def test_retried_batch_after_commit_is_a_no_op(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """Crash right after a commit: the resumed batch finds the checkpoint advanced and writes nothing."""
    sp = spec()
    t = await new_tenant(app_sessions)
    run = await run_job(app_sessions, s3, settings, t, sp, 0, CrashAt("after_commit", 3))
    assert run.crashed
    await assert_invariants(app_sessions, s3, settings, t, sp, run, 0)


# ------------------------------------------------------------------ failure modes
async def test_drops_are_gaps_and_gap_units_never_report_absence(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    clean, dropping = (
        spec(dialect="slack_history"),
        spec(dialect="slack_history", failures={"seed": 4, "drop_rate": 0.25}),
    )
    t = await new_tenant(app_sessions)
    await run_job(app_sessions, s3, settings, t, clean, 0)
    run = await run_job(app_sessions, s3, settings, t, dropping, 1)
    assert run.status is JobStatus.COMPLETED_WITH_GAPS
    recon = [
        u.recon_status for u in await units(app_sessions, t, run.job_id) if u.kind != "directory"
    ]
    assert "gap" in recon
    gap_units = {
        u.unit_key for u in await units(app_sessions, t, run.job_id) if u.recon_status == "gap"
    }
    assert gap_units
    # dropped messages are missing from this job, but no no_longer_observed was emitted from gap units
    assert (await event_items(app_sessions, t, run.job_id)).get(
        "no_longer_observed", 0
    ) == await _nlo_from_clean_units_only(app_sessions, t, run.job_id, gap_units)
    await assert_invariants(app_sessions, s3, settings, t, dropping, run, 1, oracle=False)


async def _nlo_from_clean_units_only(app_sessions: Sessions, t, job_id, gap_units: set[str]) -> int:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    from edisc_db.session import tenant_tx

    async with tenant_tx(app_sessions, t.tenant_id) as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT ji.unit_key FROM job_items ji JOIN items i ON i.id = ji.item_id"
                        " WHERE ji.job_id = :j AND i.event_kind = 'no_longer_observed'"
                    ),
                    {"j": job_id},
                )
            )
            .scalars()
            .all()
        )
    assert not set(rows) & gap_units, "absence was reported from a unit with a gap"
    return len(rows)


async def test_unavailable_files_are_recorded_gaps_and_later_availability_is_observed(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(failures={"seed": 5, "file_unavailable_rate": 0.6})
    t = await new_tenant(app_sessions)
    first = await run_job(app_sessions, s3, settings, t, sp, 0)  # never raises, never stalls
    assert first.status is JobStatus.COMPLETED_WITH_GAPS
    events = await event_items(app_sessions, t, first.job_id)
    assert events.get("file_unavailable", 0) > 0
    gaps = [u for u in await units(app_sessions, t, first.job_id) if u.file_gaps > 0]
    assert gaps and all(u.recon_status == "gap" for u in gaps)
    second = await run_job(app_sessions, s3, settings, t, sp, 1)  # expired_url files work again
    assert (await event_items(app_sessions, t, second.job_id)).get("file_became_available", 0) > 0
    await assert_invariants(app_sessions, s3, settings, t, sp, second, 1, oracle=False)


async def test_lost_conversation_is_one_observation_not_a_per_message_flood(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(dialect="slack_history", failures={"seed": 6, "inaccessible_from_epoch": {1: 1}})
    ds = Dataset(sp)
    lost_conv = ds.conversations()[1].id
    t = await new_tenant(app_sessions)
    await run_job(app_sessions, s3, settings, t, sp, 0)
    run = await run_job(app_sessions, s3, settings, t, sp, 1)
    assert run.status is JobStatus.COMPLETED_WITH_GAPS
    events = await event_items(app_sessions, t, run.job_id)
    assert (
        events.get("access_lost") == 1
    )  # ONE conversation-level observation for 3 inaccessible units
    lost_units = [
        u for u in await units(app_sessions, t, run.job_id) if u.conversation_id == lost_conv
    ]
    assert lost_units and all(u.recon_status == "access_lost" for u in lost_units)
    from sqlalchemy import text

    from edisc_db.session import tenant_tx

    async with tenant_tx(app_sessions, t.tenant_id) as s:
        flood = (
            await s.execute(
                text(
                    "SELECT count(*) FROM items i JOIN job_items ji ON ji.item_id = i.id AND ji.job_id = :j"
                    " WHERE i.event_kind = 'no_longer_observed' AND i.source_item_id LIKE :p"
                ),
                {"j": run.job_id, "p": f"%/{lost_conv}/%"},
            )
        ).scalar_one()
    assert flood == 0
    await assert_invariants(app_sessions, s3, settings, t, sp, run, 1, oracle=False)


async def test_source_that_cannot_count_ends_completed_unverified_without_absence(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(dialect="slack_history", count_mode="unavailable")
    t = await new_tenant(app_sessions)
    await run_job(app_sessions, s3, settings, t, sp, 0)
    run = await run_job(app_sessions, s3, settings, t, sp, 1)
    assert run.status is JobStatus.COMPLETED_UNVERIFIED
    assert (await event_items(app_sessions, t, run.job_id)).get("no_longer_observed", 0) == 0
    await assert_invariants(app_sessions, s3, settings, t, sp, run, 1, oracle=False)


async def _batch_events(app_sessions: Sessions, t, job_id) -> int:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    from edisc_db.session import tenant_tx

    async with tenant_tx(app_sessions, t.tenant_id) as s:
        return int(
            (
                await s.execute(
                    text(
                        "SELECT count(*) FROM custody_events WHERE stream_id = :j AND event_type = 'items_collected'"
                    ),
                    {"j": job_id},
                )
            ).scalar_one()
        )


@pytest.mark.parametrize("point", ["after_commit", "mid_transaction", "after_evidence"])
async def test_a_resumed_job_records_exactly_the_batches_of_a_clean_job(
    app_sessions: Sessions, s3: S3Client, settings: Settings, point: str
) -> None:
    """A retried batch whose checkpoint already advanced writes NOTHING: not even an empty custody event."""
    sp = spec()
    clean_t, crash_t = await new_tenant(app_sessions), await new_tenant(app_sessions)
    clean = await run_job(app_sessions, s3, settings, clean_t, sp, 0)
    crashed = await run_job(app_sessions, s3, settings, crash_t, sp, 0, CrashAt(point, 4))
    assert crashed.crashed
    assert await _batch_events(app_sessions, crash_t, crashed.job_id) == await _batch_events(
        app_sessions, clean_t, clean.job_id
    )


async def test_out_of_range_flag_lives_on_the_job_link_not_the_item(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """The same parent message is out of range for a day-1-only job and in range for a full job."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    from edisc_connector_dummy.connector import DummyConnector, scope_for_days
    from edisc_connectors_base.types import Connection
    from edisc_core.ids import new_id
    from edisc_db.session import tenant_tx
    from edisc_worker.pipeline import Pipeline

    from ...unit.dummy.conftest import RecordingLimiter

    sp = spec()
    ds = Dataset(sp)
    t = await new_tenant(app_sessions)
    conn = Connection(
        t.tenant_id,
        t.connection_id,
        "dummy",
        sp.workspace_id,
        {"spec": sp.model_dump(mode="json"), "epoch": 0},
    )
    p = Pipeline(app_sessions, s3, settings, DummyConnector(RecordingLimiter()))
    jobs = {}
    for name, first_day, days in (("day1_only", 1, 1), ("full", 0, 2)):
        job = new_id()
        scope = scope_for_days(
            "*", datetime.combine(ds.day(first_day), datetime.min.time(), tzinfo=UTC), days
        )
        await p.start_job(
            tenant_id=t.tenant_id,
            job_id=job,
            matter_id=t.matter_id,
            connection_id=t.connection_id,
            scopes=[scope],
            requested_by="tester",
        )
        await p.run(tenant_id=t.tenant_id, job_id=job, conn=conn)
        jobs[name] = job
    conv = ds.conversations()[0].id
    parent_ts = ds.out_of_range_parents(
        conv, 1, 0, datetime.combine(ds.day(1), datetime.min.time(), tzinfo=UTC)
    )[0]
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        links = dict(
            (
                await s.execute(
                    text(
                        "SELECT ji.job_id, ji.in_scope FROM job_items ji JOIN items i ON i.id = ji.item_id"
                        " WHERE i.source_item_id = :sid AND i.item_type = 'message'"
                    ),
                    {"sid": f"{sp.workspace_id}/{conv}/{parent_ts}"},
                )
            ).all()
        )
        versions = (
            await s.execute(
                text("SELECT count(*) FROM items WHERE source_item_id = :sid AND tenant_id = :t"),
                {"sid": f"{sp.workspace_id}/{conv}/{parent_ts}", "t": t.tenant_id},
            )
        ).scalar_one()
    assert links == {jobs["day1_only"]: False, jobs["full"]: True}
    assert versions == 1  # ONE item, two links with different in_scope


async def test_two_concurrent_executors_of_the_same_unit_apply_each_batch_once(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """A zombie activity attempt and its retry run the SAME unit at the same time from the same cursor.
    The checkpoint guard makes the loser's batch a no-op: the result equals a single clean run."""
    import asyncio
    from datetime import UTC, datetime

    from edisc_connector_dummy.connector import DummyConnector, scope_for_days
    from edisc_connectors_base.types import Connection
    from edisc_core.ids import new_id
    from edisc_worker.pipeline import CollectOutcome, Pipeline

    from ...unit.dummy.conftest import RecordingLimiter

    sp = spec()
    ds = Dataset(sp)
    clean_t, race_t = await new_tenant(app_sessions), await new_tenant(app_sessions)
    clean = await run_job(app_sessions, s3, settings, clean_t, sp, 0)

    conn = Connection(
        race_t.tenant_id,
        race_t.connection_id,
        "dummy",
        sp.workspace_id,
        {"spec": sp.model_dump(mode="json"), "epoch": 0},
    )
    scope = scope_for_days(
        "*", datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC), ds.n_days(0)
    )
    job = new_id()
    a = Pipeline(app_sessions, s3, settings, DummyConnector(RecordingLimiter()))
    b = Pipeline(app_sessions, s3, settings, DummyConnector(RecordingLimiter()))
    await a.start_job(
        tenant_id=race_t.tenant_id,
        job_id=job,
        matter_id=race_t.matter_id,
        connection_id=race_t.connection_id,
        scopes=[scope],
        requested_by="tester",
    )
    await a.enumerate_units(tenant_id=race_t.tenant_id, job_id=job, conn=conn)

    async def drain(p: Pipeline, unit_key: str) -> None:
        while (
            await p.collect_pages(
                tenant_id=race_t.tenant_id, job_id=job, unit_key=unit_key, conn=conn, max_pages=1
            )
            is CollectOutcome.MORE
        ):
            pass

    for unit_key in await a.pending_units(race_t.tenant_id, job):
        await asyncio.gather(drain(a, unit_key), drain(b, unit_key))
        await a.finalize_unit(tenant_id=race_t.tenant_id, job_id=job, unit_key=unit_key, conn=conn)
    status = await a.finalize_job(tenant_id=race_t.tenant_id, job_id=job)
    assert status is JobStatus.COMPLETED
    assert await _batch_events(app_sessions, race_t, job) == await _batch_events(
        app_sessions, clean_t, clean.job_id
    )
    from .conftest import JobRun

    await assert_invariants(app_sessions, s3, settings, race_t, sp, JobRun(job, status, False), 0)


async def test_multi_scope_jobs_are_rejected_at_creation(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import text

    from edisc_connector_dummy.connector import DummyConnector, scope_for_days
    from edisc_core.ids import new_id
    from edisc_db.session import tenant_tx
    from edisc_worker.pipeline import MultiScopeNotSupportedError, Pipeline

    from ...unit.dummy.conftest import RecordingLimiter

    t = await new_tenant(app_sessions)
    p = Pipeline(app_sessions, s3, settings, DummyConnector(RecordingLimiter()))
    day = datetime(2026, 1, 5, tzinfo=UTC)
    job = new_id()
    for scopes in ([scope_for_days("C1", day, 1), scope_for_days("C2", day, 2)], []):
        with pytest.raises(MultiScopeNotSupportedError, match="exactly one date-range scope"):
            await p.start_job(
                tenant_id=t.tenant_id,
                job_id=job,
                matter_id=t.matter_id,
                connection_id=t.connection_id,
                scopes=scopes,
                requested_by="tester",
            )
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        assert (
            await s.execute(text("SELECT count(*) FROM collection_jobs WHERE id = :j"), {"j": job})
        ).scalar_one() == 0
