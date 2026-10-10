"""A SYNTHETIC sealed job for report tests at sizes collection would make slow (the per-conversation
cap at 1,000 / 1,001 conversations, ADR 0018 §1, §4.6): work-unit rows and a real hash chain
(`job_started`, one `unit_reconciled` per unit through `edisc_custody.log.append`, `job_finished`, the
WORM seal), the same construction as `scripts/measure_report.py`. The report reads it exactly as it
reads a collected job. Expected values come from the parameters, never from the report."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import text
from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.log import append, seal_job_chain
from edisc_db.session import tenant_tx

from ..normalizer.harness import Sessions, Tenant

PER_TX = 500
START = date(2026, 1, 5)


def unit_keys(conversations: int, days: int = 1) -> list[str]:
    return [
        f"C{c:06d}/{(START + timedelta(days=d)).isoformat()}"
        for c in range(conversations)
        for d in range(days)
    ]


def recon_of(key: str) -> str:
    """Every 97th conversation has a gap (so the capped list has a worst-first order to check)."""
    return "gap" if int(key[1:7]) % 97 == 0 else "matched"


async def sealed_job(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, keys: list[str]
) -> uuid.UUID:
    job = new_id()
    async with tenant_tx(sessions, t.tenant_id) as s:
        await s.execute(text("INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id,"
                             " status, connector_version, requested_by) VALUES (:j, :t, :m, :c,"
                             " 'running', '0.4.0', 'synthetic')"),
                        {"j": job, "t": t.tenant_id, "m": t.matter_id, "c": t.connection_id})  # fmt: skip
        await append(s, tenant_id=t.tenant_id, stream_id=job, job_id=job, event_type="job_started",
                     actor="synthetic", payload={"connector": "dummy", "connector_version": "0.4.0",
                     "connection_id": str(t.connection_id), "scopes": [],
                     "unit_day_zone": "UTC"})  # fmt: skip
    for start in range(0, len(keys), PER_TX):
        chunk = keys[start : start + PER_TX]
        async with tenant_tx(sessions, t.tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day,"
                    " status, expected_count, collected_count, recon_status)"
                    " SELECT :t, :j, k, split_part(k, '/', 1), CAST(split_part(k, '/', 2) AS date),"
                    " 'done', 5, CASE WHEN r = 'gap' THEN 4 ELSE 5 END, r"
                    " FROM unnest(CAST(:k AS text[]), CAST(:r AS text[])) AS u(k, r)"
                ),
                {"t": t.tenant_id, "j": job, "k": chunk, "r": [recon_of(k) for k in chunk]},
            )
            for key in chunk:
                gap = recon_of(key) == "gap"
                await append(s, tenant_id=t.tenant_id, stream_id=job, job_id=job,
                             event_type="unit_reconciled", actor="synthetic",
                             payload={"unit_key": key, "expected": 5, "collected": 4 if gap else 5,
                                      "recon_status": recon_of(key), "file_gaps": 0,
                                      "no_longer_observed": 0},
                             anchor_every=1 << 30)  # fmt: skip
    status = "completed_with_gaps" if any(recon_of(k) == "gap" for k in keys) else "completed"
    async with tenant_tx(sessions, t.tenant_id) as s:
        await append(s, tenant_id=t.tenant_id, stream_id=job, job_id=job, event_type="job_finished",
                     actor="synthetic", payload={"status": status, "units": {}, "paused_ms": 0,
                     "stop_reason": None})  # fmt: skip
        await s.execute(text("UPDATE collection_jobs SET status = :s, finished_at = now()"
                             " WHERE id = :j"), {"j": job, "s": status})  # fmt: skip
    await seal_job_chain(sessions, s3, settings, tenant_id=t.tenant_id, job_id=job)
    async with tenant_tx(sessions, t.tenant_id) as s:
        await s.execute(text("UPDATE collection_jobs SET sealed_at = now() WHERE id = :j"),
                        {"j": job})  # fmt: skip
    return job
