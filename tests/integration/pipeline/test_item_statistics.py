"""A collection refreshes its items' statistics as they double (ADR 0015 §24.20): the job counts
the items it inserted in each batch's transaction, and the batch that crosses a power of two
(from 1,024) calls ``analyze_collected_items`` after its commit, once per threshold."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.schemas import JobStatus
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.pipeline import statistics_due

from ..custody.conftest import superuser
from ..normalizer.harness import new_tenant
from .conftest import run_job, spec


async def _items_analyzed(settings: Settings) -> int:
    conn = await superuser(settings)
    try:
        return int(
            await conn.fetchval(
                "SELECT analyze_count FROM pg_stat_user_tables"
                " WHERE schemaname = 'edisc' AND relname = 'items'"
            )
        )
    finally:
        await conn.close()


async def test_a_job_refreshes_its_items_statistics_once_per_doubling(
    app_sessions: async_sessionmaker[AsyncSession], s3: S3Client, settings: Settings
) -> None:
    t = await new_tenant(app_sessions)
    before = await _items_analyzed(settings)
    run = await run_job(
        app_sessions, s3, settings, t, spec(conversations=3, days=2, messages_per_unit=400), 0
    )
    assert run.status is JobStatus.COMPLETED
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        counted = (
            await s.execute(
                text("SELECT items_inserted FROM collection_jobs WHERE id = :j"), {"j": run.job_id}
            )
        ).scalar_one()
        inserted = (
            await s.execute(text("SELECT count(*) FROM items WHERE job_id = :j"), {"j": run.job_id})
        ).scalar_one()
    assert counted == inserted > 2048  # the counter is exact (one transaction per batch)
    thresholds = sum(statistics_due(n - 1, n) for n in range(1, inserted + 1))
    assert await _items_analyzed(settings) - before == thresholds
