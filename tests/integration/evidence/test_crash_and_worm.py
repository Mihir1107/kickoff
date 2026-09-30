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

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_core.time import format_utc, utc_now
from edisc_custody.recovery import complete_by_refetch, recover_job_evidence
from edisc_db.session import tenant_tx
from edisc_evidence.upload import rehash_object
from edisc_evidence.writer import EvidenceIntegrityError, EvidenceWriter, file_key

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

    report = await writer.recover_pending(tenant_id=ctx.tenant_id, job_id=ctx.job_id)
    assert (report.missing, report.completed, report.needs_refetch) == ([row.id], [], [])
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
    assert (await writer.verify(tenant_id=ctx.tenant_id, evidence_id=retry.evidence_id)).clean


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


async def _orphan(
    app_sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    ctx: Ctx,
    body: bytes,
    *,
    source_hash: str | None,
) -> tuple[object, str]:
    """A writer that died after the object was stored but before the registry recorded completion."""
    evidence_id = new_id()
    key = f"t/{ctx.tenant_id}/jobs/{ctx.job_id}/pages/{evidence_id}.json"
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until, source_sha256,"
                " source_hash_origin) VALUES (:id, :t, :j, :k, 'page', now() + interval '1 day', :h,"
                " CASE WHEN CAST(:h AS text) IS NULL THEN NULL ELSE 'collection' END)"
            ),
            {"id": evidence_id, "t": ctx.tenant_id, "j": ctx.job_id, "k": key, "h": source_hash},
        )
    await s3.put_object(
        Bucket=settings.s3_evidence_bucket,
        Key=key,
        Body=body,
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=utc_now() + timedelta(hours=1),
    )
    return evidence_id, key


async def test_recovery_completes_against_the_persisted_source_hash(
    writer: EvidenceWriter, app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    body = b'{"messages": [1, 2, 3]}'
    evidence_id, _key = await _orphan(
        app_sessions, s3, ev_settings, ctx, body, source_hash=hashlib.sha256(body).hexdigest()
    )
    report = await recover_job_evidence(
        writer,
        app_sessions,
        s3,
        ev_settings,
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        actor="finalizer",
    )
    assert report.completed == [evidence_id]
    result = await writer.verify(tenant_id=ctx.tenant_id, evidence_id=evidence_id)  # type: ignore[arg-type]
    assert result.clean
    assert await _custody_payloads(app_sessions, ctx) == [
        ("persisted_source_hash", [str(evidence_id)])
    ]


async def test_recovery_refuses_a_mismatch_with_the_persisted_source_hash(
    writer: EvidenceWriter, app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    await _orphan(
        app_sessions,
        s3,
        ev_settings,
        ctx,
        b"what storage holds",
        source_hash=hashlib.sha256(b"what the source sent").hexdigest(),
    )
    with pytest.raises(
        EvidenceIntegrityError, match="no stored version matches the persisted source hash"
    ):
        await writer.recover_pending(tenant_id=ctx.tenant_id, job_id=ctx.job_id)


async def test_no_persisted_source_hash_requires_refetch_from_the_source(
    writer: EvidenceWriter, app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx
) -> None:
    body = b'{"messages": ["refetch me"]}'
    evidence_id, _ = await _orphan(app_sessions, s3, ev_settings, ctx, body, source_hash=None)
    report = await recover_job_evidence(
        writer,
        app_sessions,
        s3,
        ev_settings,
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        actor="finalizer",
    )
    assert (report.needs_refetch, report.completed) == (
        [evidence_id],
        [],
    )  # never completed from storage bytes
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        assert (
            await s.execute(
                text("SELECT state FROM evidence_objects WHERE id = :i"), {"i": evidence_id}
            )
        ).scalar_one() == "pending"
    # a refetch that disagrees with storage is an incident
    with pytest.raises(EvidenceIntegrityError):
        await complete_by_refetch(
            writer,
            app_sessions,
            s3,
            ev_settings,
            tenant_id=ctx.tenant_id,
            job_id=ctx.job_id,
            evidence_id=evidence_id,
            source=one_shot(b"different"),
            actor="finalizer",
        )  # type: ignore[arg-type]
    written = await complete_by_refetch(
        writer,
        app_sessions,
        s3,
        ev_settings,
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        evidence_id=evidence_id,
        source=one_shot(body),
        actor="finalizer",
    )  # type: ignore[arg-type]
    assert written.sha256 == hashlib.sha256(body).hexdigest()
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        row = (
            await s.execute(
                text("SELECT state, source_hash_origin FROM evidence_objects WHERE id = :i"),
                {"i": evidence_id},
            )
        ).one()
    assert (row.state, row.source_hash_origin) == ("complete", "refetch")
    paths = [p for p, _ in await _custody_payloads(app_sessions, ctx)]
    assert paths == ["persisted_source_hash", "refetch_from_source"]


async def _custody_payloads(app_sessions: Sessions, ctx: Ctx) -> list[tuple[str, list[str]]]:
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT payload FROM custody_events WHERE stream_id = :j AND event_type = 'evidence_recovered' ORDER BY seq"
                    ),
                    {"j": ctx.job_id},
                )
            )
            .scalars()
            .all()
        )
    return [(p["recovery_path"], p.get("completed", [p.get("evidence_id")])) for p in rows]


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
    assert (await writer.verify(tenant_id=ctx.tenant_id, evidence_id=written.evidence_id)).clean
