"""The per-content advisory lock: dedicated connection, bounded hold time, waiters proceed."""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_evidence.writer import EvidenceCopyTimeoutError, EvidenceWriter, file_key

from ..conftest import Connect
from .conftest import Ctx, one_shot, rand

Sessions = async_sessionmaker[AsyncSession]


async def _advisory_locks(connect: Connect) -> int:
    su = await connect("superuser")
    try:
        return int(await su.fetchval("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))
    finally:
        await su.close()


async def _lock_connections(connect: Connect) -> int:
    su = await connect("superuser")
    try:
        return int(
            await su.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'edisc-content-lock'"
            )
        )
    finally:
        await su.close()


async def test_hung_copy_releases_the_lock_and_a_waiter_proceeds(
    app_sessions: Sessions, s3: S3Client, ev_settings: Settings, ctx: Ctx, connect: Connect
) -> None:
    settings = ev_settings.model_copy(update={"evidence_copy_timeout_seconds": 1.0})
    data = rand(4096)
    key = file_key(ctx.tenant_id, hashlib.sha256(data).hexdigest())

    hung = EvidenceWriter(app_sessions, s3, settings)
    copy_started = asyncio.Event()

    async def hanging_copy(*_: object) -> str:
        copy_started.set()
        await asyncio.sleep(3600)  # S3 never answers
        raise AssertionError("unreachable")

    hung._copy_into_worm = hanging_copy  # type: ignore[method-assign]
    healthy = EvidenceWriter(app_sessions, s3, settings)

    holder = asyncio.create_task(
        hung.write_file(
            tenant_id=ctx.tenant_id,
            job_id=ctx.job_id,
            matter_retention_until=ctx.matter_retention_until,
            stream=one_shot(data),
        )
    )
    await copy_started.wait()
    assert await _advisory_locks(connect) >= 1  # the holder has it
    started = time.monotonic()
    waiter = await healthy.write_file(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        matter_retention_until=ctx.matter_retention_until,
        stream=one_shot(data),
    )
    waited = time.monotonic() - started
    with pytest.raises(EvidenceCopyTimeoutError, match="lock released"):
        await holder

    assert waiter.storage_key == key
    assert (await healthy.verify(tenant_id=ctx.tenant_id, evidence_id=waiter.evidence_id)).clean
    assert waited < 5, f"waiter blocked {waited:.1f}s"
    await asyncio.sleep(0.2)
    assert await _lock_connections(connect) == 0  # every dedicated lock connection was closed
    assert await _advisory_locks(connect) == 0


async def test_lock_connection_is_never_taken_from_the_pool(
    writer: EvidenceWriter, ctx: Ctx, connect: Connect
) -> None:
    """While the lock is held, the connection shows up as its own backend, not a pooled app session."""
    async with writer._content_lock("probe-key"):
        assert await _lock_connections(connect) == 1
    await asyncio.sleep(0.2)
    assert await _lock_connections(connect) == 0
