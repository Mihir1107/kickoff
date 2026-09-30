"""Anchor sweeper: overdue anchors of terminated/abandoned jobs get sealed with no further writer."""

from __future__ import annotations

from datetime import timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_custody.chain import anchor_key
from edisc_custody.log import anchor_if_due, append, verify_chain
from edisc_custody.sweeper import SweepError, sweep_anchors
from edisc_db.session import tenant_tx
from edisc_evidence.worm import list_versions

from ..conftest import Connect
from .conftest import Job, new_job

Sessions = async_sessionmaker[AsyncSession]


async def _append(sessions: Sessions, job: Job, event_type: str) -> None:
    async with tenant_tx(sessions, job.tenant_id) as s:
        await append(
            s,
            tenant_id=job.tenant_id,
            stream_id=job.job_id,
            job_id=job.job_id,
            event_type=event_type,
            actor="worker",
            payload={},
        )


async def _anchor_seqs(s3: S3Client, settings: Settings, job: Job) -> list[int]:
    prefix = anchor_key(str(job.tenant_id), str(job.job_id), 0).rsplit("/", 1)[0] + "/"
    return sorted(
        [
            int(v.key.rsplit("/", 1)[1].split(".")[0])
            async for v in list_versions(s3, bucket=settings.s3_evidence_bucket, prefix=prefix)
            if not v.is_delete_marker
        ]
    )


async def test_killed_job_with_due_anchor_is_sealed_by_the_sweeper(
    sweeper_sessions: Sessions, app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """Required scenario: anchor-due set, job dies permanently, sweeper seals it, verify_chain passes."""
    job = await new_job(app_sessions)
    await _append(
        app_sessions, job, "job_started"
    )  # lifecycle => anchor_due; the worker "dies" before anchoring
    await _append(app_sessions, job, "note")
    await _append(app_sessions, job, "job_failed")
    async with tenant_tx(app_sessions, job.tenant_id) as s:  # the job is terminated for good
        await s.execute(
            text("UPDATE collection_jobs SET status = 'failed', finished_at = now() WHERE id = :j"),
            {"j": job.job_id},
        )
        due = (
            await s.execute(
                text("SELECT anchor_due FROM custody_chain_heads WHERE stream_id = :j"),
                {"j": job.job_id},
            )
        ).scalar_one()
    assert due is True
    assert await _anchor_seqs(s3, settings, job) == []
    before = await verify_chain(
        app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id
    )
    assert any("no matching WORM seal" in e for e in before.errors)  # finished but unsealed

    result = await sweep_anchors(
        sweeper_sessions, app_sessions, s3, settings, tenant_id=job.tenant_id
    )

    assert result.anchored == [anchor_key(str(job.tenant_id), str(job.job_id), 3)]
    assert await _anchor_seqs(s3, settings, job) == [3]
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT last_anchored_seq, anchor_due FROM custody_chain_heads WHERE stream_id = :j"
                ),
                {"j": job.job_id},
            )
        ).one()
    assert (row.last_anchored_seq, row.anchor_due) == (3, False)
    report = await verify_chain(
        app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id
    )
    assert report.ok, report.errors
    # a second sweep finds nothing to do
    assert (
        await sweep_anchors(sweeper_sessions, app_sessions, s3, settings, tenant_id=job.tenant_id)
    ).streams_seen == 0


async def test_idle_unanchored_tail_is_swept_only_after_the_idle_window(
    sweeper_sessions: Sessions, app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await new_job(app_sessions)
    await _append(app_sessions, job, "job_started")
    await anchor_if_due(app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id)
    await _append(app_sessions, job, "note")  # not due: below the N threshold, not lifecycle
    await _append(app_sessions, job, "note")
    assert (
        await sweep_anchors(
            sweeper_sessions,
            app_sessions,
            s3,
            settings,
            tenant_id=job.tenant_id,
            idle=timedelta(hours=1),
        )
    ).streams_seen == 0
    swept = await sweep_anchors(
        sweeper_sessions, app_sessions, s3, settings, tenant_id=job.tenant_id, idle=timedelta(0)
    )
    assert swept.streams_seen == 1
    assert await _anchor_seqs(s3, settings, job) == [1, 3]


async def test_sweeper_failures_are_raised_not_swallowed(
    sweeper_sessions: Sessions, app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    good, bad = await new_job(app_sessions), await new_job(app_sessions)
    for job in (good, bad):
        await _append(app_sessions, job, "job_started")
    # someone with bucket credentials pre-planted a different object at the anchor key
    key = anchor_key(str(bad.tenant_id), str(bad.job_id), 1)
    await s3.put_object(
        Bucket=settings.s3_evidence_bucket, Key=key, Body=canonical_json({"forged": True})
    )
    for job, expect_error in ((good, False), (bad, True)):
        if expect_error:
            with pytest.raises(SweepError) as exc:
                await sweep_anchors(
                    sweeper_sessions, app_sessions, s3, settings, tenant_id=job.tenant_id
                )
            assert "already exists with different content" in str(exc.value.exceptions[0])
        else:
            assert (
                len(
                    (
                        await sweep_anchors(
                            sweeper_sessions, app_sessions, s3, settings, tenant_id=job.tenant_id
                        )
                    ).anchored
                )
                == 1
            )


async def test_sweeper_function_exposes_ids_only_and_only_the_sweeper_may_call_it(
    connect: Connect, settings: Settings
) -> None:
    su = await connect("superuser")
    try:
        role = await su.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = 'edisc_sweeper'"
        )
        assert role is not None
        assert not any(role.values())
        owner = await su.fetchval(
            "SELECT r.rolname FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner WHERE p.proname = 'due_anchor_streams'"
        )
        assert owner == "edisc_sweeper"
        cols = await su.fetchval(
            "SELECT pg_get_function_result('due_anchor_streams(interval, integer, uuid)'::regprocedure)"
        )
        assert cols == "TABLE(tenant_id uuid, stream_id uuid)"
    finally:
        await su.close()
    app = await connect("app")
    try:
        # the app role cannot call the cross-tenant lookup, cannot become the sweeper, cannot read heads
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError, match="permission denied for function"
        ):
            await app.fetch("SELECT * FROM due_anchor_streams('0 seconds', 10, NULL)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("SET ROLE edisc_sweeper")
        assert await app.fetchval("SELECT count(*) FROM custody_chain_heads") == 0
    finally:
        await app.close()
    sweeper = await connect("sweeper")
    try:
        # the sweeper login sees ids of overdue heads only, and nothing else in the schema
        await sweeper.fetch("SELECT * FROM due_anchor_streams('0 seconds', 10, NULL)")
        for sql in (
            "SELECT last_hash FROM custody_chain_heads",
            "SELECT * FROM custody_events LIMIT 1",
            "SELECT * FROM items LIMIT 1",
            "SELECT * FROM tenants LIMIT 1",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await sweeper.fetch(sql)
    finally:
        await sweeper.close()
