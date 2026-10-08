"""Operating reports (ADR 0018 §9, §11, §13): retention of a report's objects by both routes, two
identical snapshots racing, the anchor sweeper racing a recovering seal, stuck sealing and
unroutable reports as episodes with one alert each."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now
from edisc_custody.retention_extension import extend_retention
from edisc_custody.sweeper import sweep_anchors
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_worker.report_ops import ensure_job_reports
from edisc_worker.reports import ReportRun

from ..custody.test_retention_extension import window_settings
from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..pipeline.conftest import CrashAt, SimulatedCrash, run_job, spec
from .conftest import assert_completed, drive, new_report, report_state


async def _sealed_job(
    sessions: Sessions, s3: S3Client, settings: Settings
) -> tuple[Tenant, uuid.UUID]:
    t = await new_tenant(sessions)
    return t, (await run_job(sessions, s3, settings, t, spec(), 0)).job_id


async def _owned(sessions: Sessions, t: Tenant, report_id: uuid.UUID) -> list[Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT id, kind, storage_key, version_id, retain_until FROM evidence_objects"
                        " WHERE report_id = :r AND state = 'complete' ORDER BY storage_key"
                    ),
                    {"r": report_id},
                )
            ).all()
        )


async def test_a_reports_files_and_anchors_are_extended_with_the_matter(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """Report files reach the matter by ``job_id``; the report stream's anchors (no job id) by
    ``report_id`` (report -> job -> matter). Both must be extended."""
    rs = window_settings(settings)
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    owned = await _owned(app_sessions, t, report_id)
    assert Counter(o.kind for o in owned) == Counter({"report": 5, "anchor": len(owned) - 5})
    assert any(o.kind == "anchor" for o in owned)
    before = {o.id: ensure_utc(o.retain_until) for o in owned}
    now = utc_now()
    await extend_retention(sweeper_sessions, app_sessions, s3, rs, now=now, tenant_id=t.tenant_id)
    target = effective_retain_until(rs, t.retention, now=now)
    for o in await _owned(app_sessions, t, report_id):
        until = ensure_utc(o.retain_until)
        assert until > before[o.id], o.storage_key
        assert abs((until - target).total_seconds()) < 5, (until, target)
        lock = await s3.get_object_retention(
            Bucket=rs.s3_evidence_bucket, Key=o.storage_key, VersionId=o.version_id
        )
        assert abs((ensure_utc(lock["Retention"]["RetainUntilDate"]) - until).total_seconds()) < 1


async def test_two_identical_snapshots_racing_make_one_live_report(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    a = await new_report(app_sessions, t, job_id)
    b = await new_report(app_sessions, t, job_id)
    run = ReportRun(app_sessions, s3, settings)
    statuses = await asyncio.gather(run.snapshot(t.tenant_id, a), run.snapshot(t.tenant_id, b))
    assert sorted(statuses) == ["refused", "snapshotted"]
    for r in (a, b):
        await drive(run, t.tenant_id, r)
    rows = [(await report_state(app_sessions, t.tenant_id, r))["row"] for r in (a, b)]
    assert sorted(r.status for r in rows) == ["completed", "refused"]
    assert {r.reason for r in rows if r.status == "refused"} == {"duplicate_identity"}


async def test_the_anchor_sweeper_racing_a_recovering_seal_leaves_one_anchor(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    with pytest.raises(SimulatedCrash):
        await drive(ReportRun(app_sessions, s3, settings, CrashAt("seal_start", 1)), t.tenant_id,
                    report_id)  # fmt: skip
    swept, out = await asyncio.gather(
        sweep_anchors(sweeper_sessions, app_sessions, s3, settings, tenant_id=t.tenant_id),
        drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id),
    )
    assert out["status"] == "completed"
    await assert_completed(app_sessions, s3, settings, t, job_id, report_id)
    resp = await s3.list_object_versions(
        Bucket=settings.s3_evidence_bucket, Prefix=f"custody-anchors/{t.tenant_id}/{report_id}/"
    )
    keys = Counter(v["Key"] for v in resp.get("Versions", []))
    assert keys and all(n == 1 for n in keys.values()), keys
    _ = swept


async def _episodes(sessions: Sessions, t: Tenant, subject: uuid.UUID) -> list[Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT kind, ended_at, end_reason FROM production_episodes"
                        " WHERE subject_id = :s ORDER BY started_at"
                    ),
                    {"s": subject},
                )
            ).all()
        )


async def _alerts(sessions: Sessions, t: Tenant, job_id: uuid.UUID) -> list[str]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text("SELECT kind FROM alerts WHERE job_id = :j ORDER BY created_at"),
                    {"j": job_id},
                )
            ).scalars()
        )


async def test_a_seal_that_keeps_failing_opens_one_sealing_stuck_episode(
    app_sessions: Sessions, s3: S3Client, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    run = ReportRun(app_sessions, s3, settings)
    for step in (run.snapshot, run.begin, run.files):
        await step(t.tenant_id, report_id)
    original = ReportRun._seal_once

    async def broken(self: ReportRun, *args: Any, **kwargs: Any) -> None:
        raise ConnectionError("injected: the store is unreachable")

    monkeypatch.setattr(ReportRun, "_seal_once", broken)
    for _ in range(settings.render_seal_stuck_attempts + 2):
        with pytest.raises(ConnectionError):
            await run.complete(t.tenant_id, report_id)
    assert [e.kind for e in await _episodes(app_sessions, t, report_id)] == ["sealing_stuck"]
    assert await _alerts(app_sessions, t, job_id) == ["report_sealing_stuck"]
    monkeypatch.setattr(ReportRun, "_seal_once", original)
    assert (await run.complete(t.tenant_id, report_id))["status"] == "completed"
    (episode,) = await _episodes(app_sessions, t, report_id)
    assert (episode.kind, episode.end_reason) == ("sealing_stuck", "sealed")


async def test_a_report_no_worker_can_build_opens_one_unroutable_episode(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings,
    temporal: Client,
) -> None:  # fmt: skip
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    other = {"renderer_version": "0.0.1", "toolchain_id": "none", "unicode_version": "15.0.0"}
    report_id = await new_report(app_sessions, t, job_id, identity=other)
    now = settings.model_copy(
        update={"render_unroutable_seconds": 0, "report_missing_seconds": 3600}
    )
    for _ in range(2):
        out = await ensure_job_reports(
            sweeper_sessions, app_sessions, temporal, now, tenant_id=t.tenant_id
        )
    assert out.unroutable_opened == []  # the second run found the episode open
    (episode,) = await _episodes(app_sessions, t, report_id)
    assert episode.kind == "unroutable" and episode.ended_at is None
    assert (await _alerts(app_sessions, t, job_id)).count("report_unroutable") == 1
    _ = report_state
