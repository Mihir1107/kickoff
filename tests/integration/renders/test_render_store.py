"""M15 step 3: loader and storage on the real stack (Postgres, MinIO with Object Lock). Nothing mocked
except one fault injection (a corrupted byte on the file read path).

- a sealed dummy job renders; every in-scope message is exactly one event across the stored files;
- files are locked production evidence of the job (its matter's retention), pinned by VersionId,
  with our SHA-256 as the source hash (origin "render");
- byte-identical re-renders: the same render id dedups; a new render id gives the same bytes;
- inputs are verified on read: a page hash, an item's raw_hash or file bytes that do not match fail
  the render, and nothing corrupted is stored;
- unsealed jobs are refused; roots the job does not hold are recorded, never invented.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec, FailureSpec
from edisc_connectors_base.types import Connection, ThreadParentPolicy
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_renderers.rsmf import EvidenceMismatchError, FileAttachment, RenderOptions
from edisc_worker.pipeline import CrashHooks, Pipeline
from edisc_worker.render_loader import RenderInputIntegrityError, RenderLoader, RenderRefusedError
from edisc_worker.render_store import RenderOutput, render_and_store

from ...unit.dummy.conftest import RecordingLimiter
from ...unit.renderers.emlcheck import check_eml, custom
from ..custody.conftest import superuser
from ..normalizer.harness import Sessions, Tenant, new_tenant

SPEC = DatasetSpec(
    seed=23,
    conversations=3,
    days=2,
    messages_per_unit=16,
    page_size=6,
    users=8,
    p_file=0.3,
    p_reply_prev_day=0.3,
    failures=FailureSpec(file_unavailable_rate=0.3),
)


async def _job(
    sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    t: Tenant,
    epoch: int,
    first_day: int = 0,
    policy: ThreadParentPolicy = ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD,
) -> uuid.UUID:
    ds = Dataset(SPEC)
    conn = Connection(
        t.tenant_id, t.connection_id, "dummy", SPEC.workspace_id,
        {"spec": SPEC.model_dump(mode="json"), "epoch": epoch},
    )  # fmt: skip
    start = datetime.combine(ds.day(first_day), datetime.min.time(), tzinfo=UTC)
    scope = scope_for_days("*", start, ds.n_days(epoch) - first_day, thread_parent_policy=policy)
    job_id = new_id()
    p = Pipeline(sessions, s3, settings, DummyConnector(RecordingLimiter()), CrashHooks())
    await p.start_job(
        tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
        connection_id=t.connection_id, scopes=[scope], requested_by="tester",
    )  # fmt: skip
    await p.run(tenant_id=t.tenant_id, job_id=job_id, conn=conn)
    return job_id


async def _in_scope_subjects(sessions: Sessions, t: Tenant, job_id: uuid.UUID) -> set[str]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return set(
            (
                await s.execute(
                    text(
                        "SELECT i.source_item_id FROM job_items ji JOIN items i ON i.id = ji.item_id"
                        " WHERE ji.job_id = :j AND ji.in_scope AND i.item_type = 'message'"
                    ),
                    {"j": job_id},
                )
            ).scalars()
        )


async def _read(s3: S3Client, settings: Settings, key: str, version: str) -> bytes:
    resp = await s3.get_object(Bucket=settings.s3_evidence_bucket, Key=key, VersionId=version)
    async with resp["Body"] as body:
        data: bytes = await body.read()
    return data


async def _render(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, job_id: uuid.UUID,
    render_id: uuid.UUID | None = None, options: RenderOptions | None = None,
) -> RenderOutput:  # fmt: skip
    return await render_and_store(
        sessions, s3, settings, tenant_id=t.tenant_id, job_id=job_id,
        render_id=render_id or new_id(), options=options or RenderOptions(),
    )  # fmt: skip


@pytest.fixture
async def world(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> tuple[Tenant, uuid.UUID]:
    """Epoch 0 over both days, then an epoch-1 job over day 1 onwards: edits from earlier versions,
    out-of-scope roots on day 0, unavailable files."""
    t = await new_tenant(app_sessions)
    await _job(app_sessions, s3, settings, t, epoch=0)
    return t, await _job(app_sessions, s3, settings, t, epoch=1, first_day=1)


async def test_a_sealed_job_renders_into_locked_productions(
    app_sessions: Sessions, s3: S3Client, settings: Settings, world: tuple[Tenant, uuid.UUID]
) -> None:
    t, job_id = world
    render_id = new_id()
    out = await _render(app_sessions, s3, settings, t, job_id, render_id)
    want = await _in_scope_subjects(app_sessions, t, job_id)

    # oracle: the dataset's visible messages over the job's range, independent of the database
    ds = Dataset(SPEC)
    oracle = sum(
        len(ds.visible_messages(c.id, d, 1))
        for c in ds.conversations()
        for d in range(1, ds.n_days(1))
    )
    r = out.reconciliation
    assert r.items_in == r.events_out == len(want) == oracle
    assert r.context_events > 0 and r.context_events_out_of_scope > 0
    assert r.edits > 0 and r.unavailable_attachments > 0 and r.attachments > 0
    assert out.verified_objects > 0
    assert out.job.status.value == "completed_with_gaps"  # the refused files are gaps, never clean

    primaries: Counter[str] = Counter()
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        rows = {
            row.id: row
            for row in (
                await s.execute(text("SELECT * FROM evidence_objects WHERE kind = 'production'"))
            ).all()
        }
    assert {f.evidence_id for f in out.files} == set(rows)
    retain = effective_retain_until(settings, t.retention)
    for f in out.files:
        row = rows[f.evidence_id]
        assert (row.state, row.job_id, row.source_hash_origin) == ("complete", job_id, "render")
        assert row.sha256 == row.source_sha256 == f.sha256 and row.version_id == f.version_id
        assert row.storage_key == f"t/{t.tenant_id}/productions/{render_id}/{f.name}"
        assert abs((row.retain_until - retain).total_seconds()) < 120
        head = await s3.head_object(
            Bucket=settings.s3_evidence_bucket, Key=row.storage_key, VersionId=row.version_id
        )
        assert head["ObjectLockMode"] == "COMPLIANCE"
        data = await _read(s3, settings, row.storage_key, row.version_id)
        assert hashlib.sha256(data).hexdigest() == f.sha256 and len(data) == f.size
        parsed = check_eml(data)
        assert parsed.headers["X-RSMF-CollectionId"] == str(job_id)
        assert parsed.headers["X-RSMF-CompletenessBasis"] == "source"
        (conv,) = parsed.manifest["conversations"]
        assert "type" not in conv  # live sources record no conversation metadata yet
        assert {"name": "edisc.conversation_metadata", "value": "not_collected"} in conv["custom"]
        for e in parsed.manifest["events"]:
            c = custom(e)
            if "edisc.context" not in c:
                primaries[c["edisc.source_item_id"][0]] += 1
    assert primaries == Counter(want)  # every in-scope message exactly once, across all files


async def test_re_renders_are_byte_identical_and_dedup(
    app_sessions: Sessions, s3: S3Client, settings: Settings, world: tuple[Tenant, uuid.UUID]
) -> None:
    t, job_id = world
    render_id = new_id()
    first = await _render(app_sessions, s3, settings, t, job_id, render_id)
    again = await _render(app_sessions, s3, settings, t, job_id, render_id)
    other = await _render(app_sessions, s3, settings, t, job_id)
    assert all(f.deduplicated for f in again.files)
    assert [f.evidence_id for f in again.files] == [f.evidence_id for f in first.files]
    assert [(f.name, f.sha256) for f in other.files] == [(f.name, f.sha256) for f in first.files]
    assert not any(f.deduplicated for f in other.files)
    assert again.reconciliation == first.reconciliation


async def test_roots_the_job_does_not_hold_are_recorded(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t = await new_tenant(app_sessions)
    job_id = await _job(
        app_sessions, s3, settings, t, epoch=0, first_day=1, policy=ThreadParentPolicy.REPLIES_ONLY
    )
    out = await _render(app_sessions, s3, settings, t, job_id)
    assert out.reconciliation.parents_not_rendered > 0
    assert out.reconciliation.context_events == 0  # nothing out of scope was linked
    assert out.reconciliation.items_in == len(await _in_scope_subjects(app_sessions, t, job_id))


async def test_an_unsealed_job_is_refused(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t = await new_tenant(app_sessions)
    ds = Dataset(SPEC)
    p = Pipeline(app_sessions, s3, settings, DummyConnector(RecordingLimiter()), CrashHooks())
    job_id = new_id()
    start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
    await p.start_job(
        tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
        connection_id=t.connection_id, scopes=[scope_for_days("*", start, 1)], requested_by="tester",
    )  # fmt: skip
    with pytest.raises(RenderRefusedError, match="sealed"):
        await _render(app_sessions, s3, settings, t, job_id)


async def _productions(sessions: Sessions, t: Tenant) -> list[Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT state, storage_key FROM evidence_objects WHERE kind = 'production'"
                    )
                )
            ).all()
        )


@pytest.mark.parametrize("tamper", ["page_sha256", "item_raw_hash", "derivation"])
async def test_tampered_inputs_fail_before_anything_is_stored(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tamper: str
) -> None:
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    conn = await superuser(settings)
    try:
        target = await conn.fetchrow(
            "SELECT i.id, i.evidence_object_id FROM job_items ji JOIN items i ON i.id = ji.item_id"
            " WHERE ji.job_id = $1 AND ji.in_scope AND i.item_type = 'message' ORDER BY i.id LIMIT 1",
            job_id,
        )
        assert target is not None
        if tamper == "page_sha256":
            await conn.execute(
                "UPDATE evidence_objects SET sha256 = repeat('0', 64), source_sha256 = repeat('0', 64)"
                " WHERE id = $1",
                target["evidence_object_id"],
            )
        elif tamper == "item_raw_hash":
            await conn.execute(
                "UPDATE items SET raw_hash = repeat('1', 64) WHERE id = $1", target["id"]
            )
        else:
            await conn.execute(
                "UPDATE item_derivations SET derived = jsonb_set(derived, '{text}', '\"altered\"')"
                " WHERE item_id = $1",
                target["id"],
            )
    finally:
        await conn.close()
    with pytest.raises(RenderInputIntegrityError):
        await _render(app_sessions, s3, settings, t, job_id)
    assert await _productions(app_sessions, t) == []  # pass 1 failed: nothing was written


async def test_corrupted_file_bytes_never_reach_a_stored_production(
    app_sessions: Sessions, s3: S3Client, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    original = RenderLoader.opener
    corrupted: list[str] = []

    def corrupting_opener(self: RenderLoader):  # type: ignore[no-untyped-def]
        inner = original(self)

        async def open_file(f: FileAttachment) -> AsyncIterator[bytes]:
            first = True
            async for chunk in inner(f):
                if first and not corrupted:  # the first file read: flip one byte
                    corrupted.append(f.file_id)
                    chunk = bytes([chunk[0] ^ 0xFF]) + chunk[1:]
                first = False
                yield chunk

        return open_file

    monkeypatch.setattr(RenderLoader, "opener", corrupting_opener)
    render_id = new_id()
    with pytest.raises(EvidenceMismatchError, match=corrupted[0] if corrupted else "file"):
        await _render(app_sessions, s3, settings, t, job_id, render_id)
    assert corrupted
    rows = await _productions(app_sessions, t)
    pending = [r for r in rows if r.state == "pending"]
    assert len(pending) == 1  # the file being written when the mismatch surfaced
    versions = await s3.list_object_versions(
        Bucket=settings.s3_evidence_bucket, Prefix=pending[0].storage_key
    )
    assert not versions.get("Versions") and not versions.get("DeleteMarkers")  # no object at all
    for r in rows:
        if r.state == "complete":  # files before the corrupted one are intact and verified
            assert r.storage_key != pending[0].storage_key


async def test_a_re_render_that_differs_from_the_stored_production_is_an_incident(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    from edisc_evidence.writer import EvidenceIntegrityError

    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, settings, t, epoch=0)
    render_id = new_id()
    first = await _render(app_sessions, s3, settings, t, job_id, render_id)
    conn = await superuser(settings)
    try:  # the registry now claims other bytes for one stored file
        await conn.execute(
            "UPDATE evidence_objects SET sha256 = repeat('2', 64), source_sha256 = repeat('2', 64)"
            " WHERE id = $1",
            first.files[0].evidence_id,
        )
    finally:
        await conn.close()
    with pytest.raises(EvidenceIntegrityError, match="re-render"):
        await _render(app_sessions, s3, settings, t, job_id, render_id)
