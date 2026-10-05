"""Oversized attachments as natives (ADR 0015 §11, §20) on the real stack: the server-side copy from the
pinned evidence version, one native per (render, SHA-256) however many files reference it, the
custody records (``render_natives``, each batch's ``natives_root``, the totals in
``render_completed``), the renderer never reading a native's bytes, the crash matrix at every native
boundary (simulated, and real SIGKILLs of the worker process during the copy and during the
verification read), and retention through render -> job -> matter."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.spec import DatasetSpec, FailureSpec
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now
from edisc_custody.log import verify_chain
from edisc_custody.render_files import RENDER_BATCH_EVENT, RENDER_COMPLETED
from edisc_custody.retention_extension import extend_retention
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_renderers.rsmf import FileAttachment, RenderOptions, runtime_versions
from edisc_worker.contracts import RenderRef, render_task_queue, render_workflow_id
from edisc_worker.render_loader import RenderLoader
from edisc_worker.renders import RenderRun
from edisc_worker.workflows import RenderWorkflow

from ..custody.test_retention_extension import window_settings
from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..pipeline.conftest import CrashAt, SimulatedCrash
from .conftest import drive, expected_files, new_render, render_state
from .test_render_crash_matrix import _spawn, assert_final, drive_like_the_workflow
from .test_render_store import _job

MIB = 1 << 20
BATCH = 2
# files from 768 KiB to 1.25 MiB: about half over a 1 MiB threshold (natives), half inline; some
# unavailable (placeholders next to natives); each file is reused by several messages and days
BIG = DatasetSpec(
    seed=24, conversations=2, days=2, messages_per_unit=12, page_size=6, users=8, p_file=0.35,
    file_size_min=768 << 10, file_size_span=512 << 10,
    failures=FailureSpec(file_unavailable_rate=0.2),
)  # fmt: skip
OPTIONS = RenderOptions(external_over_bytes=MIB)


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"render_files_batch_size": BATCH})


async def _natives(sessions: Sessions, t: Tenant, render_id: uuid.UUID) -> list[Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT n.*, e.kind, e.state, e.job_id AS ev_job, e.render_id AS ev_render,"
                        " e.sha256 AS ev_sha, e.size_bytes AS ev_size, e.retain_until"
                        " FROM render_natives n JOIN evidence_objects e ON e.id = n.evidence_object_id"
                        " WHERE n.render_id = :r ORDER BY n.ord"
                    ),
                    {"r": render_id},
                )
            ).all()
        )


async def _expected_natives(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, job_id: uuid.UUID
) -> dict[str, tuple[int, list[int]]]:
    """Oracle: SHA-256 -> (size, ords of the referencing files), from an in-memory rendering."""
    from edisc_renderers.rsmf import render_slice

    loader = RenderLoader(
        sessions, s3, settings, tenant_id=t.tenant_id, job_id=job_id, options=OPTIONS
    )
    await loader.load_job()
    out: dict[str, tuple[int, list[int]]] = {}
    ord_ = 0
    async for inp in loader.slices():
        for f in render_slice(inp, OPTIONS):
            for sha in sorted({a.sha256 for a in f.externals}):
                size = next(a.size for a in f.externals if a.sha256 == sha)
                out.setdefault(sha, (size, []))[1].append(ord_)
            ord_ += 1
    return out


async def _rendered(
    sessions: Sessions, s3: S3Client, settings: Settings
) -> tuple[Tenant, uuid.UUID, uuid.UUID]:
    t = await new_tenant(sessions)
    job_id = await _job(sessions, s3, settings, t, epoch=0, spec=BIG)
    render_id = await new_render(sessions, t.tenant_id, job_id, OPTIONS)
    return t, job_id, render_id


def _types(want_files: int, natives: int) -> Counter[str]:
    return Counter(
        {"render_started": 1, RENDER_BATCH_EVENT: -(-want_files // BATCH), RENDER_COMPLETED: 1}
    )


# ------------------------------------------------------------------ end to end
async def test_natives_are_copied_once_recorded_and_never_read_by_the_renderer(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    rs = _settings(settings)
    t, job_id, render_id = await _rendered(app_sessions, s3, rs)
    opened: list[str] = []
    original = RenderLoader.opener

    def recording(self: RenderLoader) -> Any:
        inner = original(self)

        async def opener(f: FileAttachment) -> AsyncIterator[bytes]:
            opened.append(f.sha256)
            async for chunk in inner(f):
                yield chunk

        return opener

    monkeypatch.setattr(RenderLoader, "opener", recording)
    result = await drive(RenderRun(app_sessions, s3, rs), t.tenant_id, render_id)
    assert result["status"] == "completed", result
    monkeypatch.undo()

    want = await expected_files(app_sessions, s3, rs, t.tenant_id, job_id, OPTIONS)
    expected = await _expected_natives(app_sessions, s3, rs, t, job_id)
    assert expected, "the dataset puts files over the threshold"
    assert any(len(ords) > 1 for _, ords in expected.values()), "a native shared by files"
    st = await assert_final(
        app_sessions, s3, rs, t, render_id, "completed", _types(len(want), len(expected)), tmp_path
    )
    assert st["files"] == want
    natives = await _natives(app_sessions, t, render_id)
    assert {n.sha256: (n.size_bytes, list(n.file_ords)) for n in natives} == expected
    assert [n.ord for n in natives] == list(range(len(natives)))
    assert not set(opened) & set(expected), "the renderer read a native"
    assert set(opened), "inline files are still streamed"
    for n in natives:  # a production of the render, under the job's matter
        assert (n.kind, n.state, n.ev_job, n.ev_render) == (
            "production",
            "complete",
            job_id,
            render_id,
        )
        assert (n.ev_sha, n.ev_size) == (n.sha256, n.size_bytes)
        assert n.storage_key == f"t/{t.tenant_id}/productions/{render_id}/natives/sha256/{n.sha256}"
        resp = await s3.get_object(
            Bucket=rs.s3_evidence_bucket, Key=n.storage_key, VersionId=n.version_id
        )
        async with resp["Body"] as body:
            assert hashlib.sha256(await body.read()).hexdigest() == n.sha256
        lock = await s3.get_object_retention(
            Bucket=rs.s3_evidence_bucket, Key=n.storage_key, VersionId=n.version_id
        )
        assert lock["Retention"]["Mode"] == "COMPLIANCE"
    assert st["productions"] == {"complete": len(want) + len(natives)}
    # custody: every batch carries its natives, the completion carries the totals
    batches = [e.payload for e in st["events"] if e.event_type == RENDER_BATCH_EVENT]
    assert sum(b["native_count"] for b in batches) == len(natives)
    assert all("natives_root" in b for b in batches)
    completed = next(e.payload for e in st["events"] if e.event_type == RENDER_COMPLETED)
    assert completed["native_count"] == len(natives) == st["row"].native_count
    assert completed["natives_root"] == st["row"].natives_root
    assert completed["reconciliation"]["external_attachments"] >= len(natives)


async def test_verify_chain_catches_an_altered_native_record(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    from ..custody.conftest import superuser

    rs = _settings(settings)
    t, _, render_id = await _rendered(app_sessions, s3, rs)
    await drive(RenderRun(app_sessions, s3, rs), t.tenant_id, render_id)
    clean = await verify_chain(
        app_sessions, s3, rs, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert clean.ok, clean.errors
    conn = await superuser(rs)
    try:
        await conn.execute("SET session_replication_role = replica")  # past the insert-only trigger
        await conn.execute(
            "UPDATE render_natives SET size_bytes = size_bytes + 1 WHERE render_id = $1 AND ord = 0",
            render_id,
        )
    finally:
        await conn.close()
    report = await verify_chain(
        app_sessions, s3, rs, tenant_id=t.tenant_id, stream_id=render_id, require_seal=True
    )
    assert not report.ok and any("natives_root" in e for e in report.errors), report.errors


# ------------------------------------------------------------------ the crash matrix
POINTS = [
    (p, n)
    for p in ("native_parts_copied", "native_copied", "native_verifying", "native_written")
    for n in (1, 2)
] + [("natives_inserted", 1)]  # this dataset's natives are all first referenced in one batch


@pytest.mark.parametrize(("point", "nth"), POINTS, ids=[f"{p}-{n}" for p, n in POINTS])
async def test_a_crash_at_every_native_boundary_resumes_exactly(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path, point: str, nth: int
) -> None:
    """``native_copied`` and ``native_verifying``: the object exists, its row is pending (the resume
    verifies and pins it); ``native_parts_copied``: the copy never completes; ``natives_inserted``:
    the native rows roll back with their batch. Each resumes with one version per key and the
    oracle's bytes and natives."""
    rs = _settings(settings)
    t, job_id, render_id = await _rendered(app_sessions, s3, rs)
    with pytest.raises(SimulatedCrash):
        await drive_like_the_workflow(
            RenderRun(app_sessions, s3, rs, CrashAt(point, nth)), t, render_id
        )
    if point in ("native_copied", "native_verifying"):
        mid = await render_state(app_sessions, t.tenant_id, render_id)
        assert mid["productions"].get("pending") == 1, mid["productions"]
    result = await drive_like_the_workflow(RenderRun(app_sessions, s3, rs), t, render_id)
    assert result["status"] == "completed", result
    want = await expected_files(app_sessions, s3, rs, t.tenant_id, job_id, OPTIONS)
    expected = await _expected_natives(app_sessions, s3, rs, t, job_id)
    st = await assert_final(
        app_sessions, s3, rs, t, render_id, "completed", _types(len(want), len(expected)), tmp_path
    )
    assert st["files"] == want
    natives = await _natives(app_sessions, t, render_id)
    assert {n.sha256: (n.size_bytes, list(n.file_ords)) for n in natives} == expected
    assert st["productions"] == {"complete": len(want) + len(expected)}


@pytest.mark.parametrize("step", ["native_parts_copied", "native_verifying"])
async def test_sigkill_during_a_native_copy_or_its_verification_read(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client, tmp_path: Path,
    step: str,
) -> None:  # fmt: skip
    """The worker PROCESS is killed with the native's multipart copy still open (parts copied, not
    completed), or in the middle of the destination's verification read. A new process finishes:
    the open upload is aborted, one object version per native key, the oracle's bytes."""
    t, job_id, render_id = await _rendered(app_sessions, s3, settings)
    queue = f"{render_task_queue(**runtime_versions())}.native-{uuid.uuid4().hex[:6]}"
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    worker = await _spawn(
        tmp_path, 1, queue, {**os.environ, "EDISC_TEST_RENDER_BARRIER": f"{step}:{barrier}"}
    )
    try:
        handle = await temporal.start_workflow(
            RenderWorkflow.run,
            RenderRef(str(t.tenant_id), str(render_id), heartbeat_timeout_seconds=3,
                      retry_initial_seconds=0.1, retry_max_seconds=0.5, max_attempts=4),
            id=render_workflow_id(str(render_id)), task_queue=queue,
        )  # fmt: skip
        reached = barrier / f"{step}.reached"
        async with asyncio.timeout(120):
            while not reached.exists():  # noqa: ASYNC110 - a file written by another process
                await asyncio.sleep(0.02)
        worker.send_signal(signal.SIGKILL)
        await worker.wait()
        async with tenant_tx(app_sessions, t.tenant_id) as s:
            pending = (
                await s.execute(
                    text(
                        "SELECT storage_key, upload_id FROM evidence_objects WHERE render_id = :r"
                        " AND kind = 'production' AND state = 'pending'"
                    ),
                    {"r": render_id},
                )
            ).one()
        assert "/natives/sha256/" in pending.storage_key
        versions = await s3.list_object_versions(
            Bucket=settings.s3_evidence_bucket, Prefix=pending.storage_key
        )
        held = len(versions.get("Versions", []))
        assert held == (0 if step == "native_parts_copied" else 1), (
            versions
        )  # where the kill landed
        if step == "native_parts_copied":
            assert pending.upload_id
        worker = await _spawn(tmp_path, 2, queue, dict(os.environ))
        async with asyncio.timeout(120):
            result = await handle.result()
    finally:
        if worker.returncode is None:
            worker.send_signal(signal.SIGKILL)
            await worker.wait()
    assert result["status"] == "completed"
    uploads = await s3.list_multipart_uploads(
        Bucket=settings.s3_evidence_bucket, Prefix=pending.storage_key
    )
    assert not uploads.get("Uploads"), "the copy the kill left open was aborted"
    want = await expected_files(app_sessions, s3, settings, t.tenant_id, job_id, OPTIONS)
    expected = await _expected_natives(app_sessions, s3, settings, t, job_id)
    batch = settings.render_files_batch_size
    st = await assert_final(
        app_sessions, s3, settings, t, render_id, "completed",
        Counter({"render_started": 1, RENDER_BATCH_EVENT: -(-len(want) // batch),
                 RENDER_COMPLETED: 1}),
        tmp_path,
    )  # fmt: skip
    assert st["files"] == want
    assert st["productions"] == {"complete": len(want) + len(expected)}


# ------------------------------------------------------------------ retention
async def test_natives_are_extended_with_the_matter(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    rs = window_settings(settings)
    t, _, render_id = await _rendered(app_sessions, s3, settings)
    await drive(RenderRun(app_sessions, s3, settings), t.tenant_id, render_id)
    before = {
        n.sha256: ensure_utc(n.retain_until) for n in await _natives(app_sessions, t, render_id)
    }
    assert before
    now = utc_now()
    await extend_retention(sweeper_sessions, app_sessions, s3, rs, now=now, tenant_id=t.tenant_id)
    after = {
        n.sha256: (ensure_utc(n.retain_until), n)
        for n in await _natives(app_sessions, t, render_id)
    }
    target = effective_retain_until(rs, t.retention, now=now)
    for sha, (until, n) in after.items():
        assert until > before[sha]
        assert abs((until - target).total_seconds()) < 5, (until, target)
        lock = await s3.get_object_retention(
            Bucket=rs.s3_evidence_bucket, Key=n.storage_key, VersionId=n.version_id
        )
        assert abs((ensure_utc(lock["Retention"]["RetainUntilDate"]) - until).total_seconds()) < 1


# ------------------------------------------------------------------ protections of the copy
async def _first_native_source(
    sessions: Sessions, t: Tenant, job_id: uuid.UUID
) -> tuple[str, str, str, int]:
    """(source key, pinned VersionId, SHA-256, size) of the largest collected file of the job."""
    async with tenant_tx(sessions, t.tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT storage_key, version_id, sha256, size_bytes FROM evidence_objects"
                    " WHERE kind = 'file' AND state = 'complete' AND size_bytes > :m"
                    " ORDER BY size_bytes DESC LIMIT 1"
                ),
                {"m": MIB},
            )
        ).one()
    return row.storage_key, row.version_id, row.sha256, row.size_bytes


async def test_a_copy_that_does_not_hash_to_the_source_is_never_completed(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    from edisc_evidence.writer import EvidenceIntegrityError, EvidenceWriter

    t, job_id, render_id = await _rendered(app_sessions, s3, settings)
    key, version, sha, size = await _first_native_source(app_sessions, t, job_id)
    writer = EvidenceWriter(app_sessions, s3, settings)
    with pytest.raises(EvidenceIntegrityError, match="reads"):
        await writer.write_native(
            tenant_id=t.tenant_id, job_id=job_id, render_id=render_id, sha256="0" * 64, size=size,
            source_key=key, source_version_id=version, matter_retention_until=t.retention,
        )  # fmt: skip
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        state = (
            await s.execute(
                text("SELECT state FROM evidence_objects WHERE storage_key LIKE :k"),
                {"k": f"%/natives/sha256/{'0' * 64}"},
            )
        ).scalar_one()
    assert state == "pending"  # never completed with bytes that are not the source's
    assert sha != "0" * 64


async def test_a_second_version_at_a_native_key_is_an_incident(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    from edisc_evidence.writer import EvidenceIntegrityError

    rs = _settings(settings)
    t, _, render_id = await _rendered(app_sessions, s3, rs)
    with pytest.raises(SimulatedCrash):  # the object exists, its row is pending
        await drive_like_the_workflow(
            RenderRun(app_sessions, s3, rs, CrashAt("native_copied", 1)), t, render_id
        )
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        key = (
            await s.execute(
                text(
                    "SELECT storage_key FROM evidence_objects WHERE render_id = :r"
                    " AND state = 'pending' AND storage_key LIKE '%/natives/%'"
                ),
                {"r": render_id},
            )
        ).scalar_one()
    await s3.put_object(Bucket=rs.s3_evidence_bucket, Key=key, Body=b"a shadow written outside")
    with pytest.raises(EvidenceIntegrityError, match="versions of one native"):
        await drive(RenderRun(app_sessions, s3, rs), t.tenant_id, render_id)


async def test_the_copy_reads_the_pinned_source_version_not_the_latest(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """A newer version shadows a collected file's key: the native is still the pinned bytes."""
    rs = _settings(settings)
    t, job_id, render_id = await _rendered(app_sessions, s3, rs)
    key, _, sha, _ = await _first_native_source(app_sessions, t, job_id)
    await s3.put_object(Bucket=rs.s3_evidence_bucket, Key=key, Body=b"x" * (2 * MIB))
    result = await drive(RenderRun(app_sessions, s3, rs), t.tenant_id, render_id)
    assert result["status"] == "completed", result
    natives = {n.sha256: n for n in await _natives(app_sessions, t, render_id)}
    n = natives[sha]
    resp = await s3.get_object(
        Bucket=rs.s3_evidence_bucket, Key=n.storage_key, VersionId=n.version_id
    )
    async with resp["Body"] as body:
        assert hashlib.sha256(await body.read()).hexdigest() == sha


async def test_a_native_missing_from_the_batches_fails_the_render(
    app_sessions: Sessions, s3: S3Client, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Natives written but not committed with their batch: the final reconciliation of the recorded
    natives against the references refuses to mark the render rendered."""
    import dataclasses

    from edisc_renderers.rsmf import ReconciliationError

    rs = _settings(settings)
    t, _, render_id = await _rendered(app_sessions, s3, rs)
    original = RenderRun._commit_batch

    async def without_natives(
        self: RenderRun, tenant_id: Any, rid: Any, files: Any, size: int
    ) -> None:
        await original(
            self, tenant_id, rid, [dataclasses.replace(f, natives=()) for f in files], size
        )

    monkeypatch.setattr(RenderRun, "_commit_batch", without_natives)
    await RenderRun(app_sessions, s3, rs).begin(t.tenant_id, render_id)
    with pytest.raises(ReconciliationError, match="not written"):
        await RenderRun(app_sessions, s3, rs).render_files(t.tenant_id, render_id)
    st = await render_state(app_sessions, t.tenant_id, render_id)
    assert st["row"].status == "rendering"
