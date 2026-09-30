"""SIGKILL mid-upload, and the ADR 0002 WORM test against real Object Lock."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_core.time import format_utc
from edisc_db.session import tenant_tx
from edisc_evidence.upload import rehash_object
from edisc_evidence.writer import EvidenceWriter, file_key

from .conftest import PART, Ctx, one_shot, rand

Sessions = async_sessionmaker[AsyncSession]
ROOT = Path(__file__).resolve().parents[3]


async def _run_and_kill(kind: str, ctx: Ctx) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.integration.evidence.kill_target",
        kind,
        str(ctx.tenant_id),
        str(ctx.job_id),
        format_utc(ctx.matter_retention_until),
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
    assert line.strip() == b"READY", (line, await proc.stderr.read() if proc.stderr else b"")
    os.kill(proc.pid, signal.SIGKILL)
    assert await proc.wait() == -signal.SIGKILL


async def test_sigkill_mid_page_upload_leaves_no_object_and_is_recoverable(
    writer: EvidenceWriter, app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    await _run_and_kill("page", ctx)
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        row = (
            await s.execute(
                text("SELECT * FROM evidence_objects WHERE job_id = :j"), {"j": ctx.job_id}
            )
        ).one()
    assert row.state == "pending"
    assert row.upload_id is not None  # write-ahead recorded the in-flight upload before any part
    with pytest.raises(ClientError):  # no completed object
        await s3.head_object(Bucket=ev_settings.s3_evidence_bucket, Key=row.storage_key)
    parts = await s3.list_parts(
        Bucket=ev_settings.s3_evidence_bucket, Key=row.storage_key, UploadId=row.upload_id
    )
    assert len(parts.get("Parts", [])) >= 2  # the upload really was mid-flight

    counts = await writer.recover_pending(tenant_id=ctx.tenant_id, job_id=ctx.job_id)
    assert counts == {"completed": 0, "missing": 1, "left_pending": 0}
    with pytest.raises(ClientError) as exc:
        await s3.list_parts(
            Bucket=ev_settings.s3_evidence_bucket, Key=row.storage_key, UploadId=row.upload_id
        )
    assert exc.value.response["Error"]["Code"] == "NoSuchUpload"

    retry = await writer.write_page(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(b'{"messages": []}'),
    )
    assert await writer.verify(tenant_id=ctx.tenant_id, evidence_id=retry.evidence_id)


async def test_sigkill_mid_file_staging_leaves_no_worm_object_and_retry_succeeds(
    writer: EvidenceWriter, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    await _run_and_kill("file", ctx)
    listed = await s3.list_objects_v2(
        Bucket=ev_settings.s3_evidence_bucket, Prefix=f"t/{ctx.tenant_id}/files/"
    )
    assert (
        listed.get("KeyCount", 0) == 0
    )  # nothing reached WORM; the staging upload is expired by lifecycle
    data = rand(PART + 10)
    ok = await writer.write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(data),
    )
    assert ok.storage_key == file_key(ctx.tenant_id, hashlib.sha256(data).hexdigest())


async def test_recover_completes_an_object_whose_writer_died_after_upload(
    writer: EvidenceWriter, app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    """Crash between CompleteMultipartUpload/PutObject and the registry update: object exists, row pending."""
    body = b'{"messages": [1, 2, 3]}'
    key = f"t/{ctx.tenant_id}/jobs/{ctx.job_id}/pages/orphan.json"
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until)"
                " VALUES (gen_random_uuid(), :t, :j, :k, 'page', now() + interval '1 day')"
            ),
            {"t": ctx.tenant_id, "j": ctx.job_id, "k": key},
        )
    await s3.put_object(
        Bucket=ev_settings.s3_evidence_bucket,
        Key=key,
        Body=body,
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=ctx.matter_retention_until - timedelta(days=29, hours=23),
        ChecksumSHA256=__import__("base64").b64encode(hashlib.sha256(body).digest()).decode(),
    )
    counts = await writer.recover_pending(tenant_id=ctx.tenant_id, job_id=ctx.job_id)
    assert counts["completed"] == 1
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        row = (
            await s.execute(
                text("SELECT state, sha256 FROM evidence_objects WHERE storage_key = :k"),
                {"k": key},
            )
        ).one()
    assert (row.state, row.sha256) == ("complete", hashlib.sha256(body).hexdigest())


async def test_worm_version_cannot_be_deleted_shortened_or_overwritten(
    writer: EvidenceWriter, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    """ADR 0002: target the VERSION (a plain DELETE only adds a delete marker and proves nothing)."""
    bucket = ev_settings.s3_evidence_bucket
    written = await writer.write_page(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(rand(PART + 1)),
    )
    head = await s3.head_object(Bucket=bucket, Key=written.storage_key)
    version = head["VersionId"]
    retention_before = (
        await s3.get_object_retention(Bucket=bucket, Key=written.storage_key, VersionId=version)
    )["Retention"]

    # (a) delete the specific version -> denied
    with pytest.raises(ClientError) as exc:
        await s3.delete_object(Bucket=bucket, Key=written.storage_key, VersionId=version)
    # AWS answers AccessDenied; MinIO answers InvalidRequest "Object is WORM protected". Either is a refusal,
    # and (c) below proves the version survived.
    err = exc.value.response["Error"]
    assert err["Code"] == "AccessDenied" or "WORM protected" in err.get("Message", ""), err
    with pytest.raises(ClientError):  # COMPLIANCE ignores governance bypass
        await s3.delete_object(
            Bucket=bucket,
            Key=written.storage_key,
            VersionId=version,
            BypassGovernanceRetention=True,
        )
    # (b) shorten retention -> denied, with or without governance bypass
    shorter = retention_before["RetainUntilDate"] - timedelta(hours=1)
    for bypass in (False, True):
        with pytest.raises(ClientError):
            await s3.put_object_retention(
                Bucket=bucket,
                Key=written.storage_key,
                VersionId=version,
                Retention={"Mode": "COMPLIANCE", "RetainUntilDate": shorter},
                BypassGovernanceRetention=bypass,
            )
    with pytest.raises(ClientError):  # nor downgrade the mode
        await s3.put_object_retention(
            Bucket=bucket,
            Key=written.storage_key,
            VersionId=version,
            Retention={
                "Mode": "GOVERNANCE",
                "RetainUntilDate": retention_before["RetainUntilDate"],
            },
        )
    # overwrite with If-None-Match -> 412
    with pytest.raises(ClientError) as exc:
        await s3.put_object(
            Bucket=bucket, Key=written.storage_key, Body=b"tampered", IfNoneMatch="*"
        )
    assert exc.value.response["Error"]["Code"] == "PreconditionFailed"
    # (c) the original version still re-hashes to the recorded hash; retention unchanged
    assert await rehash_object(s3, bucket=bucket, key=written.storage_key, version_id=version) == (
        written.sha256,
        written.size,
    )
    after = (
        await s3.get_object_retention(Bucket=bucket, Key=written.storage_key, VersionId=version)
    )["Retention"]
    assert after == retention_before
    assert await writer.verify(tenant_id=ctx.tenant_id, evidence_id=written.evidence_id)
