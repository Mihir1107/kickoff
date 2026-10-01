"""M6 evidence writer: sizes and part boundaries, dedup, integrity, crash handling, bounded memory."""

from __future__ import annotations

import asyncio
import hashlib
import tracemalloc
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_evidence.upload import rehash_object
from edisc_evidence.writer import EvidenceIntegrityError, EvidenceWriter, file_key

from .conftest import PART, Ctx, MiB, chunks, expected_sha, make_ctx, one_shot, rand

Sessions = async_sessionmaker[AsyncSession]
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


async def _registry(sessions: Sessions, ctx: Ctx, evidence_id: object) -> object:
    async with tenant_tx(sessions, ctx.tenant_id) as s:
        return (
            await s.execute(
                text("SELECT * FROM evidence_objects WHERE id = :i"), {"i": evidence_id}
            )
        ).one()


async def _etag(s3: S3Client, settings: Settings, key: str) -> str:
    return str((await s3.head_object(Bucket=settings.s3_evidence_bucket, Key=key))["ETag"]).strip(
        '"'
    )


# ------------------------------------------------------------------ sizes / part boundaries (pages)
@pytest.mark.parametrize(
    ("size", "parts"),
    [(0, 0), (1, 0), (PART - 1, 0), (PART, 0), (PART + 1, 2), (12 * PART + 7, 13)],
    ids=["zero", "one-byte", "part-1", "exactly-part", "part+1", "many-parts"],
)
async def test_page_sizes_and_part_boundaries(
    writer: EvidenceWriter,
    app_sessions: Sessions,
    s3: S3Client,
    ev_settings: Settings,
    ctx: Ctx,
    size: int,
    parts: int,
) -> None:
    written = await writer.write_page(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=chunks(size),
    )
    assert written.size == size
    assert written.sha256 == (EMPTY_SHA if size == 0 else expected_sha(size))
    etag = await _etag(s3, ev_settings, written.storage_key)
    assert etag.endswith(f"-{parts}") if parts else "-" not in etag  # single PUT vs multipart
    row = await _registry(app_sessions, ctx, written.evidence_id)
    assert (row.state, row.sha256, row.size_bytes, row.kind) == (
        "complete",
        written.sha256,
        size,
        "page",
    )  # type: ignore[attr-defined]
    head = await s3.head_object(Bucket=ev_settings.s3_evidence_bucket, Key=written.storage_key)
    assert head["ObjectLockMode"] == "COMPLIANCE"
    assert (
        await rehash_object(s3, bucket=ev_settings.s3_evidence_bucket, key=written.storage_key)
    ) == (written.sha256, size)
    assert (await writer.verify(tenant_id=ctx.tenant_id, evidence_id=written.evidence_id)).clean


# ------------------------------------------------------------------ files: staging -> content-addressed WORM
@pytest.mark.parametrize(
    "size", [0, PART, PART + 1, 13 * MiB], ids=["zero", "exactly-part", "part+1", "multipart-copy"]
)
async def test_file_is_content_addressed_verified_and_staging_is_removed(
    writer: EvidenceWriter, s3: S3Client, ev_settings: Settings, ctx: Ctx, size: int
) -> None:
    data = rand(size)
    written = await writer.write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(data),
    )
    digest = hashlib.sha256(data).hexdigest()
    assert (written.sha256, written.size, written.deduplicated) == (digest, size, False)
    assert written.storage_key == file_key(ctx.tenant_id, digest)
    head = await s3.head_object(Bucket=ev_settings.s3_evidence_bucket, Key=written.storage_key)
    assert head["ObjectLockMode"] == "COMPLIANCE"
    if size > ev_settings.evidence_single_copy_max_bytes:
        assert (
            str(head["ETag"]).strip('"').endswith("-3")
        )  # UploadPartCopy path, verified by re-read
    assert (
        await rehash_object(s3, bucket=ev_settings.s3_evidence_bucket, key=written.storage_key)
    ) == (digest, size)
    staged = await s3.list_objects_v2(
        Bucket=ev_settings.s3_staging_bucket, Prefix=f"t/{ctx.tenant_id}/staging/"
    )
    assert staged.get("KeyCount", 0) == 0


async def test_same_content_is_stored_once_per_tenant_and_never_across_tenants(
    writer: EvidenceWriter, app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    data = rand(PART + 3)
    first = await writer.write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(data),
    )
    # another job of the same tenant, twice concurrently
    other_job = await make_ctx(app_sessions)  # a different tenant
    second_job = new_id()
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:  # a second job under the SAME tenant
        await s.execute(
            text(
                "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status, connector_version, requested_by)"
                " SELECT :new, tenant_id, matter_id, connection_id, 'running', '0.1.0', 't' FROM collection_jobs WHERE id = :j"
            ),
            {"new": second_job, "j": ctx.job_id},
        )
    results = await asyncio.gather(
        *(
            writer.write_file(
                tenant_id=ctx.tenant_id,
                job_id=second_job,
                matter_retention_until=ctx.matter_retention_until,
                stream=one_shot(data),
            )
            for _ in range(2)
        )
    )
    assert all(
        r.deduplicated and r.evidence_id == first.evidence_id and r.storage_key == first.storage_key
        for r in results
    )
    versions = await s3.list_object_versions(
        Bucket=ev_settings.s3_evidence_bucket, Prefix=first.storage_key
    )
    assert len(versions.get("Versions", [])) == 1  # never a second (shadowing) version
    # a different tenant never shares the object
    foreign = await writer.write_file(
        tenant_id=other_job.tenant_id,
        job_id=other_job.job_id,
        matter_retention_until=other_job.matter_retention_until,
        stream=one_shot(data),
    )
    assert not foreign.deduplicated
    assert foreign.storage_key != first.storage_key


async def test_dedup_only_ever_extends_retention(
    app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    short = ev_settings.model_copy(
        update={
            "evidence_retention_override_days": None,
            "evidence_retention_override_seconds": None,
            "evidence_retention_window_days": 1,
        }
    )
    longer = ev_settings.model_copy(
        update={
            "evidence_retention_override_days": None,
            "evidence_retention_override_seconds": None,
            "evidence_retention_window_days": 2,
        }
    )
    data = rand(1024)
    first = await EvidenceWriter(app_sessions, s3, longer).write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(data),
    )
    r1 = (
        await s3.get_object_retention(Bucket=ev_settings.s3_evidence_bucket, Key=first.storage_key)
    )["Retention"]["RetainUntilDate"]
    # a shorter window never shortens it
    await EvidenceWriter(app_sessions, s3, short).write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(data),
    )
    r2 = (
        await s3.get_object_retention(Bucket=ev_settings.s3_evidence_bucket, Key=first.storage_key)
    )["Retention"]["RetainUntilDate"]
    assert r2 == r1
    # a later write with a longer target extends object and registry together
    await asyncio.sleep(1.1)
    await EvidenceWriter(app_sessions, s3, longer).write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(data),
    )
    r3 = (
        await s3.get_object_retention(Bucket=ev_settings.s3_evidence_bucket, Key=first.storage_key)
    )["Retention"]["RetainUntilDate"]
    assert r3 > r1
    row = await _registry(app_sessions, ctx, first.evidence_id)
    assert abs((row.retain_until - r3).total_seconds()) < 1  # type: ignore[attr-defined]


async def test_mismatching_object_at_content_key_is_an_integrity_incident(
    writer: EvidenceWriter, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    data = rand(2048)
    key = file_key(ctx.tenant_id, hashlib.sha256(data).hexdigest())
    await s3.put_object(
        Bucket=ev_settings.s3_evidence_bucket, Key=key, Body=b"planted by someone else"
    )
    with pytest.raises(EvidenceIntegrityError):
        await writer.write_file(
            tenant_id=ctx.tenant_id,
            job_id=ctx.job_id,
            matter_retention_until=ctx.matter_retention_until,
            stream=one_shot(data),
        )


# ------------------------------------------------------------------ crashes (in-process)
async def test_page_failure_mid_upload_aborts_and_retry_succeeds(
    writer: EvidenceWriter, app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    with pytest.raises(ConnectionError):
        await writer.write_page(
            tenant_id=ctx.tenant_id,
            job_id=ctx.job_id,
            matter_retention_until=ctx.matter_retention_until,
            stream=chunks(8 * PART, fail_after=3 * PART),
        )
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        row = (
            await s.execute(
                text("SELECT * FROM evidence_objects WHERE job_id = :j AND kind = 'page'"),
                {"j": ctx.job_id},
            )
        ).one()
    assert row.state == "missing"
    assert row.upload_id is not None
    with pytest.raises(ClientError) as exc:  # the multipart upload was aborted
        await s3.list_parts(
            Bucket=ev_settings.s3_evidence_bucket, Key=row.storage_key, UploadId=row.upload_id
        )
    assert exc.value.response["Error"]["Code"] == "NoSuchUpload"
    with pytest.raises(ClientError):  # and no completed object exists
        await s3.head_object(Bucket=ev_settings.s3_evidence_bucket, Key=row.storage_key)
    retry = await writer.write_page(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=chunks(8 * PART),
    )
    assert retry.sha256 == expected_sha(8 * PART)


async def test_file_failure_mid_stream_leaves_no_worm_object_and_retry_is_idempotent(
    writer: EvidenceWriter, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    size = 3 * PART
    key = file_key(ctx.tenant_id, expected_sha(size, seed=7))
    with pytest.raises(ConnectionError):
        await writer.write_file(
            tenant_id=ctx.tenant_id,
            job_id=ctx.job_id,
            matter_retention_until=ctx.matter_retention_until,
            stream=chunks(size, seed=7, fail_after=2 * PART),
        )
    with pytest.raises(ClientError):
        await s3.head_object(Bucket=ev_settings.s3_evidence_bucket, Key=key)
    ok = await writer.write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=chunks(size, seed=7),
    )
    again = await writer.write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=chunks(size, seed=7),
    )
    assert (ok.storage_key, ok.deduplicated, again.deduplicated, again.evidence_id) == (
        key,
        False,
        True,
        ok.evidence_id,
    )


# ------------------------------------------------------------------ bounded memory
@pytest.mark.parametrize("kind", ["page", "file"])
async def test_memory_stays_bounded_on_a_200_mib_stream(
    writer: EvidenceWriter, ctx: Ctx, kind: str
) -> None:
    """Peak Python memory is a small constant number of parts, independent of object size."""
    fn = writer.write_page if kind == "page" else writer.write_file
    peaks = {}
    for size in (20 * MiB, 200 * MiB):
        tracemalloc.start()
        try:
            written = await fn(
                tenant_id=ctx.tenant_id,
                job_id=ctx.job_id,
                matter_retention_until=ctx.matter_retention_until,
                stream=chunks(size, seed=size),
            )
            peaks[size] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert written.size == size
    assert peaks[200 * MiB] < 4 * PART, (
        f"peak {peaks[200 * MiB] / MiB:.1f} MiB with {PART // MiB} MiB parts"
    )
    assert abs(peaks[200 * MiB] - peaks[20 * MiB]) < MiB, f"memory grows with size: {peaks}"


async def test_retention_is_the_rolling_window_capped_locally(
    ev_settings: Settings, writer: EvidenceWriter, s3: S3Client, ctx: Ctx
) -> None:
    written = await writer.write_page(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(b"{}"),
    )
    until = (
        await s3.get_object_retention(
            Bucket=ev_settings.s3_evidence_bucket, Key=written.storage_key
        )
    )["Retention"]["RetainUntilDate"]
    from edisc_core.time import utc_now

    caps = [
        timedelta(days=d) for d in [ev_settings.evidence_retention_override_days] if d is not None
    ] + [
        timedelta(seconds=s)
        for s in [ev_settings.evidence_retention_override_seconds]
        if s is not None
    ]
    assert caps, "integration tests must run with a retention override"
    assert until <= utc_now() + min(caps) + timedelta(minutes=1)
