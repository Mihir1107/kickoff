"""Custody fixtures: build real jobs (pages in WORM, items, batch events, anchors) through the app role."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_hash, canonical_json
from edisc_core.idempotency import idempotency_key
from edisc_core.ids import new_id
from edisc_core.jsonpath import build
from edisc_core.settings import Settings
from edisc_core.time import utc_now
from edisc_custody.chain import compute_event_hash, hashed_fields
from edisc_custody.log import anchor_if_due, append, append_batch, seal_job_chain
from edisc_db.session import create_tenant, tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_evidence.worm import put_immutable

ANCHOR_EVERY = 8


@dataclass
class Job:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    anchors: list[str] = field(default_factory=list)


async def new_job(sessions: async_sessionmaker[AsyncSession]) -> Job:
    tenant_id, matter_id, conn_id, job_id = new_id(), new_id(), new_id(), new_id()
    await create_tenant(
        sessions,
        tenant_id=tenant_id,
        name="T",
        subdomain=f"c-{secrets.token_hex(6)}",
        kms_key_ref="local:k",
    )
    async with tenant_tx(sessions, tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES (:m, :t, 'M', :r)"
            ),
            {"m": matter_id, "t": tenant_id, "r": utc_now() + timedelta(days=30)},
        )
        await s.execute(
            text(
                "INSERT INTO connections (id, tenant_id, source, external_org_id, status)"
                " VALUES (:c, :t, 'dummy', 'org', 'active')"
            ),
            {"c": conn_id, "t": tenant_id},
        )
        await s.execute(
            text(
                "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status, connector_version,"
                " requested_by) VALUES (:j, :t, :m, :c, 'running', '0.1.0', 'tester')"
            ),
            {"j": job_id, "t": tenant_id, "m": matter_id, "c": conn_id},
        )
    return Job(tenant_id, job_id)


async def lifecycle(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    job: Job,
    event_type: str,
) -> None:
    async with tenant_tx(sessions, job.tenant_id) as s:
        await append(
            s,
            tenant_id=job.tenant_id,
            stream_id=job.job_id,
            job_id=job.job_id,
            event_type=event_type,
            actor="tester",
            payload={"note": event_type},
            anchor_every=ANCHOR_EVERY,
        )
    key = await anchor_if_due(sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id)
    if key:
        job.anchors.append(key)


async def collect_batch(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    job: Job,
    *,
    unit_key: str,
    n_items: int,
) -> None:
    """Mimics the M11 pipeline: page -> WORM (write-ahead), then ONE transaction for items + event + links."""
    page = {
        "messages": [
            {"ts": f"{i}.{secrets.token_hex(3)}", "text": f"msg {i} {secrets.token_hex(4)}"}
            for i in range(n_items)
        ]
    }
    body = canonical_json(page)
    ev_id = new_id()
    key = f"raw/{job.tenant_id}/{job.job_id}/{ev_id}.json"
    async with tenant_tx(sessions, job.tenant_id) as s:
        retention = (
            await s.execute(
                text(
                    "SELECT m.retention_until FROM collection_jobs j JOIN matters m ON m.id = j.matter_id WHERE j.id = :j"
                ),
                {"j": job.job_id},
            )
        ).scalar_one()
        retain = effective_retain_until(settings, retention)
        await s.execute(
            text(
                "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until,"
                " source_sha256, source_hash_origin) VALUES (:id, :t, :j, :k, 'page', :r, :h, 'collection')"
            ),
            {
                "id": ev_id,
                "t": job.tenant_id,
                "j": job.job_id,
                "k": key,
                "r": retain,
                "h": hashlib.sha256(body).hexdigest(),
            },
        )
    stored = await put_immutable(
        s3, bucket=settings.s3_evidence_bucket, key=key, body=body, retain_until=retain
    )

    async with tenant_tx(sessions, job.tenant_id) as s:
        await s.execute(
            text(
                "UPDATE evidence_objects SET state = 'complete', sha256 = :h, size_bytes = :n,"
                " version_id = :v, completed_at = now() WHERE id = :id"
            ),
            {"h": stored.sha256, "n": stored.size, "v": stored.version_id, "id": ev_id},
        )
        await s.execute(
            text(
                "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day)"
                " VALUES (:t, :j, :u, 'C1', '2026-01-01') ON CONFLICT DO NOTHING"
            ),
            {"t": job.tenant_id, "j": job.job_id, "u": unit_key},
        )
        pairs, item_ids = [], []
        for i, msg in enumerate(page["messages"]):
            content_hash = canonical_hash({"text": msg["text"]})
            source_item_id = f"W/C1/{msg['ts']}"
            ikey = idempotency_key(job.tenant_id, "dummy", source_item_id, content_hash)
            item_id = new_id()
            await s.execute(
                text(
                    "INSERT INTO items (id, tenant_id, job_id, source, source_item_id, version, item_type, content_hash,"
                    " raw_hash, evidence_object_id, storage_key, json_path, connector_version, normalizer_version,"
                    " idempotency_key) VALUES (:id, :t, :j, 'dummy', :sid, 1, 'message', :ch, :rh, :ev, :k, :p,"
                    " '0.1.0', '0.1.0', :ik)"
                ),
                {
                    "id": item_id,
                    "t": job.tenant_id,
                    "j": job.job_id,
                    "sid": source_item_id,
                    "ch": content_hash,
                    "rh": canonical_hash(msg),
                    "ev": ev_id,
                    "k": key,
                    "p": build("messages", i),
                    "ik": ikey,
                },
            )
            pairs.append((ikey, content_hash))
            item_ids.append(item_id)
        event = await append_batch(
            s,
            tenant_id=job.tenant_id,
            job_id=job.job_id,
            unit_key=unit_key,
            page_evidence_id=ev_id,
            page_sha256=stored.sha256,
            items=pairs,
            actor="worker",
            anchor_every=ANCHOR_EVERY,
        )
        for item_id in item_ids:
            await s.execute(
                text(
                    "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id)"
                    " VALUES (:t, :j, :i, :u, :e)"
                ),
                {"t": job.tenant_id, "j": job.job_id, "i": item_id, "u": unit_key, "e": event.id},
            )
    anchor = await anchor_if_due(
        sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id
    )
    if anchor:
        job.anchors.append(anchor)


async def finish(
    sessions: async_sessionmaker[AsyncSession], s3: S3Client, settings: Settings, job: Job
) -> None:
    await lifecycle(sessions, s3, settings, job, "job_finished")
    async with tenant_tx(sessions, job.tenant_id) as s:
        await s.execute(
            text(
                "UPDATE collection_jobs SET status = 'completed', finished_at = now() WHERE id = :j"
            ),
            {"j": job.job_id},
        )
    job.anchors.append(
        await seal_job_chain(sessions, s3, settings, tenant_id=job.tenant_id, job_id=job.job_id)
    )


async def run_job(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    batches: int = 20,
    items_per_batch: int = 5,
    finalize: bool = True,
) -> Job:
    job = await new_job(sessions)
    await lifecycle(sessions, s3, settings, job, "job_started")
    for b in range(batches):
        await collect_batch(
            sessions,
            s3,
            settings,
            job,
            unit_key=f"C1/2026-01-{1 + b % 3:02d}",
            n_items=items_per_batch,
        )
    if finalize:
        await finish(sessions, s3, settings, job)
    return job


async def superuser(settings: Settings) -> asyncpg.Connection:
    """A DBA with full power: triggers off (session_replication_role = replica), RLS bypassed."""
    conn = await asyncpg.connect(
        settings.pg_dsn("superuser"), server_settings={"search_path": "edisc,pg_temp"}
    )
    await conn.execute("SET session_replication_role = replica")
    return conn


async def rewrite_chain_from(
    conn: asyncpg.Connection, stream_id: uuid.UUID, seq: int, new_payload: dict[str, object]
) -> None:
    """The strongest DB attack: change event ``seq`` and recompute every later hash + the head, so the
    chain is internally consistent again. Only an external anchor can expose this."""
    rows = await conn.fetch(
        "SELECT * FROM custody_events WHERE stream_id = $1 AND seq >= $2 ORDER BY seq",
        stream_id,
        seq,
    )
    prev = rows[0]["prev_hash"]
    for i, row in enumerate(rows):
        payload = new_payload if i == 0 else dict(json.loads(row["payload"]))
        fields = hashed_fields(
            tenant_id=row["tenant_id"],
            stream_id=row["stream_id"],
            job_id=row["job_id"],
            seq=row["seq"],
            event_type=row["event_type"],
            actor=row["actor"],
            item_id=row["item_id"],
            payload=payload,
            created_at=row["created_at"],
        )
        new_hash = compute_event_hash(prev, fields)
        await conn.execute(
            "UPDATE custody_events SET payload = $2::jsonb, prev_hash = $3, event_hash = $4 WHERE id = $1",
            row["id"],
            json.dumps(fields["payload"]),
            prev,
            new_hash,
        )
        prev = new_hash
    await conn.execute(
        "UPDATE custody_chain_heads SET last_hash = $2 WHERE stream_id = $1", stream_id, prev
    )
