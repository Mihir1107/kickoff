"""A collection report's lifecycle and custody stream (ADR 0018 §9, §11, §13; M16 step 4).

A crash (simulated like SIGKILL: nothing after the point runs, an open transaction rolls back) at every
boundary of a report, at its first and, where it repeats, its second occurrence; then a fresh run
resumes from the database alone. Every point ends with the stored bytes equal to an independent
in-memory build from the same snapshot, every custody and audit event once, the report sealed and its
chain verified, no pending registry row, one object version per key. Also: the refused and failure
paths, an identical identity, a divergence's alert and audit, and identity routing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from typing import Any

import pytest
from sqlalchemy import text
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import verify_chain
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_worker.reports import ReportIntegrityError, ReportRun

from ..normalizer.harness import Sessions, Tenant, new_job, new_tenant
from ..pipeline.conftest import CrashAt, SimulatedCrash, run_job, spec
from .conftest import assert_completed, drive, new_report, report_state

SINGLE = [
    "snapshot_taken", "snapshot_tx", "after_snapshot", "begin_verified", "begin_tx",
    "begin_committed", "generated_tx", "generated_committed", "seal_start", "after_seal_anchor",
    "seal_tx", "sealed",
]  # fmt: skip
PER_FILE = ["file:units.jsonl", "file:observations.jsonl", "file:renders.jsonl", "file:report.json"]
REPEATED = ["mid_upload", "file_stored", "file_tx", "file_recorded"]
POINTS = [(p, 1) for p in SINGLE + PER_FILE] + [(p, n) for p in REPEATED for n in (1, 2)]


async def _sealed_job(
    sessions: Sessions, s3: S3Client, settings: Settings, **kw: Any
) -> tuple[Tenant, uuid.UUID]:
    t = await new_tenant(sessions)
    run = await run_job(sessions, s3, settings, t, spec(**kw), 0)
    return t, run.job_id


async def _versions(s3: S3Client, settings: Settings, prefix: str) -> Counter[str]:
    resp = await s3.list_object_versions(Bucket=settings.s3_evidence_bucket, Prefix=prefix)
    assert not resp.get("DeleteMarkers")
    return Counter(v["Key"] for v in resp.get("Versions", []))


async def _one_version_per_key(
    s3: S3Client, settings: Settings, t: Tenant, report_id: uuid.UUID
) -> None:
    for prefix in (
        f"t/{t.tenant_id}/reports/{report_id}/",
        f"custody-anchors/{t.tenant_id}/{report_id}/",
    ):
        counts = await _versions(s3, settings, prefix)
        assert counts and all(n == 1 for n in counts.values()), counts


async def test_a_report_is_stored_recorded_and_sealed(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert out["status"] == "completed" and out["clean"] is True
    st = await assert_completed(app_sessions, s3, settings, t, job_id, report_id)
    started = st["events"][0].payload
    assert started["job"]["id"] == str(job_id) and started["job"]["verified"] is True
    assert (
        started["job"]["seal"]["version_id"]
        and started["snapshot_digest"] == st["row"].snapshot_digest
    )
    assert started["identity"] == {"renderer_version": st["row"].renderer_version,
                                   "toolchain_id": "none", "unicode_version": st["row"].unicode_version,
                                   "paper": "letter"}  # fmt: skip
    await _one_version_per_key(s3, settings, t, report_id)
    # the job's own chain was never appended to
    job_chain = await verify_chain(app_sessions, s3, settings, tenant_id=t.tenant_id,
                                   stream_id=job_id, require_seal=True)  # fmt: skip
    assert job_chain.ok, job_chain.errors


@pytest.mark.parametrize(("point", "nth"), POINTS, ids=[f"{p}-{n}" for p, n in POINTS])
async def test_a_crash_at_every_boundary_resumes_exactly(
    app_sessions: Sessions, s3: S3Client, settings: Settings, point: str, nth: int
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    with pytest.raises(SimulatedCrash):
        await drive(
            ReportRun(app_sessions, s3, settings, CrashAt(point, nth)), t.tenant_id, report_id
        )
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert out["status"] == "completed", out
    await assert_completed(app_sessions, s3, settings, t, job_id, report_id)
    await _one_version_per_key(s3, settings, t, report_id)


async def test_a_crash_after_an_object_is_written_but_before_its_row_completes(
    app_sessions: Sessions, s3: S3Client, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    original = EvidenceWriter._complete
    fired: list[int] = []

    async def crash_once(self: EvidenceWriter, *args: Any, **kwargs: Any) -> Any:
        if not fired:
            fired.append(1)
            raise SimulatedCrash("after the object was written")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(EvidenceWriter, "_complete", crash_once)
    with pytest.raises(SimulatedCrash):
        await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    monkeypatch.setattr(EvidenceWriter, "_complete", original)
    assert (await report_state(app_sessions, t.tenant_id, report_id))["evidence"].get(
        "pending"
    ) == 1
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert out["status"] == "completed"
    await assert_completed(app_sessions, s3, settings, t, job_id, report_id)
    await _one_version_per_key(s3, settings, t, report_id)


async def test_a_job_that_is_not_sealed_is_refused_and_sealed(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t = await new_tenant(app_sessions)
    job_id = await new_job(app_sessions, t)
    report_id = await new_report(app_sessions, t, job_id)
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert (out["status"], out["reason"]) == ("refused", "job_not_sealed")
    st = await report_state(app_sessions, t.tenant_id, report_id)
    assert st["types"] == ["report_refused"] and st["audits"] == ["audit.report_refused"]
    verified = await verify_chain(app_sessions, s3, settings, tenant_id=t.tenant_id,
                                  stream_id=report_id, require_seal=True)  # fmt: skip
    assert verified.ok, verified.errors


async def test_an_identical_identity_is_refused_and_never_replaces_the_first(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """Two requests whose snapshots are equal (nothing changed between them) are one identity:
    the second is refused. (A completed report moves the tenant audit head, so a later request is
    a new snapshot and a new report: earlier reports are never replaced.)"""
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    first = await new_report(app_sessions, t, job_id)
    second = await new_report(app_sessions, t, job_id, request_reason="again, same inputs")
    run = ReportRun(app_sessions, s3, settings)
    assert await run.snapshot(t.tenant_id, first) == "snapshotted"
    assert await run.snapshot(t.tenant_id, second) == "refused"
    out = await drive(run, t.tenant_id, second)
    assert (out["status"], out["reason"]) == ("refused", "duplicate_identity")
    assert (await drive(run, t.tenant_id, first))["status"] == "completed"
    later = await new_report(app_sessions, t, job_id, request_reason="after the first completed")
    assert (await drive(run, t.tenant_id, later))["status"] == "completed"
    rows = [
        (await report_state(app_sessions, t.tenant_id, r))["row"] for r in (first, second, later)
    ]
    assert [r.status for r in rows] == ["completed", "refused", "completed"]
    # the job's own evidence, unchanged by the first report's files and anchors
    assert rows[2].snapshot["evidence"] == rows[0].snapshot["evidence"]


@pytest.mark.parametrize("point", ["fail_tx", "fail_committed", "seal_tx", "sealed"])
async def test_a_failing_report_ends_failed_and_sealed_even_through_a_crash(
    app_sessions: Sessions, s3: S3Client, settings: Settings, point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ReportIntegrityError("injected: the build disagrees with what was recorded")

    monkeypatch.setattr(ReportRun, "files", broken)
    with pytest.raises(SimulatedCrash):
        await drive(
            ReportRun(app_sessions, s3, settings, CrashAt(point, 1)), t.tenant_id, report_id
        )
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert (out["status"], out["reason"]) == ("failed", "ReportIntegrityError")
    st = await report_state(app_sessions, t.tenant_id, report_id)
    assert st["types"] == ["report_started", "report_failed"] and st["audits"] == [
        "audit.report_failed"
    ]
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        alerts = (
            (await s.execute(text("SELECT kind FROM alerts WHERE job_id = :j"), {"j": job_id}))
            .scalars()
            .all()
        )
    assert alerts == ["report_failed"]


async def test_a_divergence_is_alerted_and_audited_once_and_the_report_not_clean(
    app_sessions: Sessions, s3: S3Client, settings: Settings, connect: Any
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    conn = await connect("superuser")
    try:
        await conn.execute(
            "UPDATE edisc.work_units SET collected_count = collected_count + 1 WHERE job_id = $1"
            " AND unit_key = (SELECT min(unit_key) FROM edisc.work_units WHERE job_id = $1"
            " AND kind = 'conversation_day')",
            job_id,
        )
    finally:
        await conn.close()
    report_id = await new_report(app_sessions, t, job_id)
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert out["status"] == "completed" and out["clean"] is False
    st = await assert_completed(app_sessions, s3, settings, t, job_id, report_id)
    assert st["row"].divergence_count == 1
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        alerts = (
            (await s.execute(text("SELECT kind FROM alerts WHERE job_id = :j"), {"j": job_id}))
            .scalars()
            .all()
        )
        audits = (
            await s.execute(
                text(
                    "SELECT count(*) FROM custody_events WHERE stream_id = :t"
                    " AND event_type = 'audit.report_divergence'"
                ),
                {"t": t.tenant_id},
            )
        ).scalar_one()
    assert alerts == ["report_divergence"] and audits == 1


async def test_a_report_for_another_runtime_is_never_built_here(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    other = {"renderer_version": "9.9.9", "toolchain_id": "none", "unicode_version": "15.0.0"}
    report_id = await new_report(app_sessions, t, job_id, identity=other)
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert (out["status"], out["reason"]) == ("failed", "ReportIntegrityError")
    st = await report_state(app_sessions, t.tenant_id, report_id)
    assert st["types"] == ["report_failed"] and st["row"].snapshot is None


async def test_a_recorded_file_the_rebuild_does_not_reproduce_fails_the_report(
    app_sessions: Sessions, s3: S3Client, settings: Settings, connect: Any
) -> None:
    """An earlier attempt recorded units.jsonl; the record is then altered. The resumed attempt
    rebuilds the file, finds it differs from its record, and the report fails (never generated)."""
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    with pytest.raises(SimulatedCrash):
        await drive(ReportRun(app_sessions, s3, settings, CrashAt("file:observations.jsonl", 1)),
                    t.tenant_id, report_id)  # fmt: skip
    conn = await connect("superuser")
    try:
        await conn.execute("SET session_replication_role = replica")  # past the insert-only guard
        await conn.execute(
            "UPDATE edisc.report_files SET rows = rows + 1 WHERE report_id = $1 AND ord = 0",
            report_id,
        )
    finally:
        await conn.close()
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert (out["status"], out["reason"]) == ("failed", "ReportIntegrityError")
    st = await report_state(app_sessions, t.tenant_id, report_id)
    assert "report_generated" not in st["types"] and st["types"][-1] == "report_failed"
    # it failed at the altered record: nothing after it was stored
    assert st["evidence"] == {"complete": 1} and [f.name for f in st["files"]] == ["units.jsonl"]


async def test_a_recorded_file_list_unlike_the_build_never_becomes_generated(
    app_sessions: Sessions, s3: S3Client, settings: Settings, connect: Any
) -> None:
    """Every file rebuilds to its record, but the report has one more recorded file than it builds:
    only the check before ``report_generated`` sees that."""
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    with pytest.raises(SimulatedCrash):
        await drive(ReportRun(app_sessions, s3, settings, CrashAt("generated_tx", 1)),
                    t.tenant_id, report_id)  # fmt: skip
    conn = await connect("superuser")
    try:
        await conn.execute("SET session_replication_role = replica")
        await conn.execute(
            "INSERT INTO edisc.report_files (tenant_id, report_id, ord, name, media_type,"
            " evidence_object_id, version_id, sha256, size_bytes, rows)"
            " SELECT tenant_id, report_id, 4, 'extra.json', media_type, evidence_object_id,"
            " version_id, sha256, size_bytes, rows FROM edisc.report_files"
            " WHERE report_id = $1 AND ord = 3",
            report_id,
        )
        await conn.execute("UPDATE edisc.reports SET files_done = 5 WHERE id = $1", report_id)
    finally:
        await conn.close()
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert (out["status"], out["reason"]) == ("failed", "ReportIntegrityError")
    assert (
        "report_generated"
        not in (await report_state(app_sessions, t.tenant_id, report_id))["types"]
    )


async def test_two_executors_of_one_report_append_each_event_once(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """A zombie attempt and its retry (ADR 0015 §14): both run begin, then both seal, at once. The
    status fence and the seal's guard let exactly one of each act; the other returns."""
    t, job_id = await _sealed_job(app_sessions, s3, settings)
    report_id = await new_report(app_sessions, t, job_id)
    a, b = ReportRun(app_sessions, s3, settings), ReportRun(app_sessions, s3, settings)
    assert await a.snapshot(t.tenant_id, report_id) == "snapshotted"
    begun = await asyncio.gather(a.begin(t.tenant_id, report_id), b.begin(t.tenant_id, report_id))
    assert begun == ["generating", "generating"]
    assert await a.files(t.tenant_id, report_id) == "generated"
    sealed = await asyncio.gather(
        a.complete(t.tenant_id, report_id), b.complete(t.tenant_id, report_id)
    )
    assert [x["status"] for x in sealed] == ["completed", "completed"]
    await assert_completed(app_sessions, s3, settings, t, job_id, report_id)
