"""Stale-upload sweeper (ADR 0012 section 7): abandoned pending evidence of unfinished jobs is
recovered with custody; live uploads, finished jobs and other tenants are left alone."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.recovery import sweep_stale_uploads
from edisc_db.session import tenant_tx

from ..conftest import Connect
from .conftest import Ctx, make_ctx
from .test_crash_and_worm import _orphan

Sessions = async_sessionmaker[AsyncSession]


async def _state(sessions: Sessions, ctx: Ctx, evidence_id: object) -> str:
    async with tenant_tx(sessions, ctx.tenant_id) as s:
        state: str = (
            await s.execute(
                text("SELECT state FROM evidence_objects WHERE id = :e"), {"e": evidence_id}
            )
        ).scalar_one()
    return state


async def test_abandoned_upload_of_an_unfinished_job_is_recovered_with_custody(
    app_sessions: Sessions,
    sweeper_sessions: Sessions,
    s3: S3Client,
    ev_settings: Settings,
    ctx: Ctx,
) -> None:
    body = b'{"messages": ["stale"]}'
    evidence_id, _ = await _orphan(
        app_sessions, s3, ev_settings, ctx, body, source_hash=hashlib.sha256(body).hexdigest()
    )
    # a live upload (younger than copy timeout + 1 h) is never touched
    untouched = await sweep_stale_uploads(
        sweeper_sessions, app_sessions, s3, ev_settings, tenant_id=ctx.tenant_id
    )
    assert untouched.jobs == [] and await _state(app_sessions, ctx, evidence_id) == "pending"

    swept = await sweep_stale_uploads(
        sweeper_sessions,
        app_sessions,
        s3,
        ev_settings,
        min_age=timedelta(0),
        tenant_id=ctx.tenant_id,
    )
    assert swept.jobs == [f"{ctx.tenant_id}/{ctx.job_id}"]
    assert await _state(app_sessions, ctx, evidence_id) == "complete"
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        events = (
            await s.execute(
                text("SELECT event_type, actor FROM custody_events WHERE stream_id = :j"),
                {"j": ctx.job_id},
            )
        ).all()
    assert [(e.event_type, e.actor) for e in events] == [
        ("evidence_recovered", "stale-upload-sweeper")
    ]


async def test_finished_jobs_are_not_swept(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, ev_settings: Settings
) -> None:
    ctx = await make_ctx(app_sessions)
    body = b'{"messages": ["done"]}'
    evidence_id, _ = await _orphan(
        app_sessions, s3, ev_settings, ctx, body, source_hash=hashlib.sha256(body).hexdigest()
    )
    async with tenant_tx(app_sessions, ctx.tenant_id) as s:
        await s.execute(
            text("UPDATE collection_jobs SET finished_at = now() WHERE id = :j"), {"j": ctx.job_id}
        )
    swept = await sweep_stale_uploads(
        sweeper_sessions,
        app_sessions,
        s3,
        ev_settings,
        min_age=timedelta(0),
        tenant_id=ctx.tenant_id,
    )
    assert swept.jobs == [] and await _state(app_sessions, ctx, evidence_id) == "pending"


async def test_only_the_sweeper_login_can_list_stale_uploads(connect: Connect) -> None:
    app = await connect("app")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.fetch("SELECT * FROM stale_pending_evidence(interval '0', 10)")
    finally:
        await app.close()
