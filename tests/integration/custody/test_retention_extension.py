"""Retention extension job (ADR 0002 amendment), over simulated time against real Postgres and MinIO.

- a validated export with no jobs keeps its retention above the floor for as long as its client is open;
- evidence of an active matter, including another matter's evidence it references through its items, is
  extended but never past the matter's retention date;
- a closed matter's or closed client's evidence is never extended;
- extensions happen only below the floor (no call per run).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now
from edisc_custody.log import append_batch
from edisc_custody.retention_extension import extend_retention
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter

from .conftest import collect_batch, run_job

Sessions = async_sessionmaker[AsyncSession]
DAY = timedelta(days=1)


def window_settings(settings: Settings) -> Settings:
    """Production-shaped retention (90-day window, 60-day floor) instead of the test stack's seconds."""
    return settings.model_copy(
        update={
            "evidence_retention_override_seconds": None,
            "evidence_retention_override_days": None,
            "evidence_retention_window_days": 90,
            "evidence_retention_extend_floor_days": 60,
        }
    )


async def _export(
    sessions: Sessions, s3: S3Client, settings: Settings, tenant: uuid.UUID, client: uuid.UUID
) -> uuid.UUID:
    """A validated export (status ready, locked zip) with no job ever using it."""
    data = new_id().bytes * 100
    key = f"exports/{tenant}/{new_id()}"
    await s3.put_object(Bucket=settings.s3_staging_bucket, Key=key, Body=data)
    written = await EvidenceWriter(sessions, s3, settings).lock_staged(
        tenant_id=tenant, staging_key=key, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )
    export, connection = new_id(), new_id()
    async with tenant_tx(sessions, tenant) as s:
        await s.execute(
            text(
                "INSERT INTO connections (id, tenant_id, client_id, source, external_org_id, status)"
                " VALUES (:i, :t, :c, 'slack_export', 'x', 'active')"
            ),
            {"i": connection, "t": tenant, "c": client},
        )
        await s.execute(
            text(
                "INSERT INTO slack_exports (id, tenant_id, client_id, declared_size, limits, staging_key,"
                " created_by) VALUES (:i, :t, :c, :n, '{}', :k, 'tests')"
            ),
            {"i": export, "t": tenant, "c": client, "n": len(data), "k": key},
        )
        await s.execute(
            text("UPDATE slack_exports SET status = 'locking' WHERE id = :i"), {"i": export}
        )
        await s.execute(
            text(
                "UPDATE slack_exports SET status = 'validating', sha256 = :h, size_bytes = :n,"
                " evidence_object_id = :e, version_id = :v, locked_at = now() WHERE id = :i"
            ),
            {
                "h": written.sha256,
                "n": len(data),
                "e": written.evidence_id,
                "v": written.version_id,
                "i": export,
            },
        )
        await s.execute(
            text(
                "UPDATE slack_exports SET status = 'ready', entry_count = 0, detected_tier = 'public_only',"
                " connection_id = :c, validated_at = now() WHERE id = :i"
            ),
            {"c": connection, "i": export},
        )
    return written.evidence_id


async def _retain(
    sessions: Sessions, tenant: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    async with tenant_tx(sessions, tenant) as s:
        rows = (
            await s.execute(
                text("SELECT id, retain_until FROM evidence_objects WHERE id = ANY(:i)"), {"i": ids}
            )
        ).all()
    return {r.id: ensure_utc(r.retain_until) for r in rows}


async def _evidence_of_job(
    sessions: Sessions, tenant: uuid.UUID, job: uuid.UUID
) -> list[uuid.UUID]:
    async with tenant_tx(sessions, tenant) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT id FROM evidence_objects WHERE job_id = :j AND state = 'complete'"
                    ),
                    {"j": job},
                )
            ).scalars()
        )


async def test_retention_is_kept_above_the_floor_while_owners_are_active(
    settings: Settings, app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client
) -> None:
    rs = window_settings(settings)
    start = utc_now()

    # matter C (closed later) owns a job; matter A (active, retention 200 days) references one of
    # C's pages through its own items
    c_job = await run_job(app_sessions, s3, rs, batches=2, items_per_batch=2, finalize=False)
    tenant = c_job.tenant_id
    a_matter, a_job, client_closed = new_id(), new_id(), new_id()
    async with tenant_tx(app_sessions, tenant) as s:
        default_client = (
            await s.execute(text("SELECT id FROM clients WHERE is_default"))
        ).scalar_one()
        conn = (await s.execute(text("SELECT id FROM connections LIMIT 1"))).scalar_one()
        await s.execute(
            text(
                "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES (:m, :t, 'A', :r)"
            ),
            {"m": a_matter, "t": tenant, "r": start + 200 * DAY},
        )
        await s.execute(
            text(
                "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status,"
                " connector_version, requested_by) VALUES (:j, :t, :m, :c, 'running', '0.1.0', 't')"
            ),
            {"j": a_job, "t": tenant, "m": a_matter, "c": conn},
        )
        shared_page, shared_items = (
            await s.execute(
                text(
                    "SELECT evidence_object_id, array_agg(id) FROM items WHERE job_id = :j"
                    " GROUP BY evidence_object_id ORDER BY evidence_object_id LIMIT 1"
                ),
                {"j": c_job.job_id},
            )
        ).one()
        await s.execute(
            text(
                "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day)"
                " VALUES (:t, :j, 'C1/2026-01-01', 'C1', '2026-01-01')"
            ),
            {"t": tenant, "j": a_job},
        )
        page_sha = (
            await s.execute(
                text("SELECT sha256 FROM evidence_objects WHERE id = :e"), {"e": shared_page}
            )
        ).scalar_one()
        pairs = [
            (r.idempotency_key, r.content_hash)
            for r in (
                await s.execute(
                    text("SELECT idempotency_key, content_hash FROM items WHERE id = ANY(:i)"),
                    {"i": list(shared_items)},
                )
            ).all()
        ]
        event = await append_batch(
            s, tenant_id=tenant, job_id=a_job, unit_key="C1/2026-01-01", page_evidence_id=shared_page,
            page_sha256=page_sha, items=pairs,
            actor="tests",
        )  # fmt: skip
        for item in shared_items:
            await s.execute(
                text(
                    "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id)"
                    " VALUES (:t, :j, :i, 'C1/2026-01-01', :e)"
                ),
                {"t": tenant, "j": a_job, "i": item, "e": event.id},
            )
        await s.execute(
            text("INSERT INTO clients (id, tenant_id, name) VALUES (:c, :t, 'Gone')"),
            {"c": client_closed, "t": tenant},
        )
    from types import SimpleNamespace

    a = SimpleNamespace(tenant_id=tenant, job_id=a_job, anchors=[])
    await collect_batch(app_sessions, s3, rs, a, unit_key="C1/2026-01-02", n_items=2)  # type: ignore[arg-type]
    export_open = await _export(app_sessions, s3, rs, tenant, default_client)
    export_closed = await _export(app_sessions, s3, rs, tenant, client_closed)
    async with tenant_tx(app_sessions, tenant) as s:  # close matter C and the second client
        await s.execute(
            # a long retention date: only being closed keeps C's evidence from being extended
            text("UPDATE matters SET closed_at = now(), closed_by = 'tests', retention_until = :r WHERE id = "
                 "(SELECT matter_id FROM collection_jobs WHERE id = :j)"),
            {"j": c_job.job_id, "r": start + 300 * DAY},
        )  # fmt: skip
        await s.execute(
            text("UPDATE clients SET closed_at = now(), closed_by = 'tests' WHERE id = :c"),
            {"c": client_closed},
        )

    a_evidence = await _evidence_of_job(app_sessions, tenant, a_job)
    c_evidence = [
        e for e in await _evidence_of_job(app_sessions, tenant, c_job.job_id) if e != shared_page
    ]
    watched = [*a_evidence, shared_page, *c_evidence, export_open, export_closed]
    initial = await _retain(app_sessions, tenant, watched)

    extensions = 0
    for day in range(0, 401, 10):
        now = start + day * DAY
        result = await extend_retention(
            sweeper_sessions, app_sessions, s3, rs, now=now, tenant_id=tenant
        )
        extensions += result.extended["export"]
        r = await _retain(app_sessions, tenant, watched)
        # the export with no jobs: always above the floor while its client is open
        assert r[export_open] >= now + 60 * DAY, (day, r[export_open])
        # active matter A (and C's page it references): above min(floor, A's retention date), never past it
        for e in [*a_evidence, shared_page]:
            assert r[e] >= min(now + 60 * DAY, start + 200 * DAY) - DAY, (day, e)
            assert r[e] <= start + 200 * DAY + DAY
        # closed owners: untouched
        for e in [*c_evidence, export_closed]:
            assert r[e] == initial[e], (day, e)

    # ~400 days / (90 - 60) days between extensions: never one call per run
    assert 10 <= extensions <= 16, extensions
    stored = await s3.get_object_retention(
        Bucket=rs.s3_evidence_bucket,
        Key=(await _key(app_sessions, tenant, export_open)),
        VersionId=await _version(app_sessions, tenant, export_open),
    )
    assert ensure_utc(stored["Retention"]["RetainUntilDate"]) >= (
        await _retain(app_sessions, tenant, [export_open])
    )[export_open] - timedelta(seconds=1)
    async with tenant_tx(app_sessions, tenant) as s:
        audits = (
            await s.execute(
                text(
                    "SELECT count(*) FROM custody_events WHERE stream_id = :t"
                    " AND event_type = 'audit.retention_extended'"
                ),
                {"t": tenant},
            )
        ).scalar_one()
    assert audits >= extensions


async def _key(sessions: Sessions, tenant: uuid.UUID, evidence: uuid.UUID) -> str:
    async with tenant_tx(sessions, tenant) as s:
        return str(
            (
                await s.execute(
                    text("SELECT storage_key FROM evidence_objects WHERE id = :e"), {"e": evidence}
                )
            ).scalar_one()
        )


async def _version(sessions: Sessions, tenant: uuid.UUID, evidence: uuid.UUID) -> str:
    async with tenant_tx(sessions, tenant) as s:
        return str(
            (
                await s.execute(
                    text("SELECT version_id FROM evidence_objects WHERE id = :e"), {"e": evidence}
                )
            ).scalar_one()
        )
