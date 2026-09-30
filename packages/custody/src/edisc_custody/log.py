"""Custody log on Postgres + WORM anchors (ADR 0003).

Write path
    ``append`` / ``append_batch`` run INSIDE the caller's tenant transaction (so a batch event commits
    atomically with its items). The chain head row is locked ``FOR UPDATE``: gapless seq under any
    number of concurrent writers. If the event is a lifecycle event or ``anchor_every`` events have
    passed since the last anchor, ``anchor_due`` is set on the head in the same transaction.

    After commit, the caller runs ``anchor_if_due``: it seals the current head as a COMPLIANCE-locked
    object and records it. The flag survives crashes, so a missed anchor is written by the next caller.

Verify path
    ``verify_chain`` streams events in seq order, recomputes hashes and batch Merkle roots from the
    items table, and checks every version of every anchor object in the bucket (listed from S3, never
    from the DB, so a DB-level attacker cannot hide anchors).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now
from edisc_custody.chain import (
    BATCH_EVENT,
    GENESIS_HASH,
    LIFECYCLE_EVENTS,
    Anchor,
    ChainVerifier,
    EventRecord,
    VerificationReport,
    anchor_document,
    anchor_key,
    anchor_prefix,
    compute_event_hash,
    hashed_fields,
)
from edisc_custody.merkle import batch_root
from edisc_db.session import tenant_tx
from edisc_evidence.retention import effective_retain_until
from edisc_evidence.worm import get_bytes, list_versions, put_immutable


@dataclass(frozen=True)
class AppendedEvent:
    id: uuid.UUID
    stream_id: uuid.UUID
    seq: int
    event_hash: str
    anchor_due: bool


async def append(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    stream_id: uuid.UUID,
    event_type: str,
    actor: str,
    payload: dict[str, Any],
    job_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
    anchor_every: int = 8,
    created_at: datetime | None = None,
) -> AppendedEvent:
    """Append one event to ``stream_id`` inside the caller's (tenant-scoped) transaction."""
    created = ensure_utc(created_at) if created_at else utc_now()
    await session.execute(
        text(
            "INSERT INTO custody_chain_heads (stream_id, tenant_id, last_seq, last_hash)"
            " VALUES (:s, :t, 0, :g) ON CONFLICT (stream_id) DO NOTHING"
        ),
        {"s": stream_id, "t": tenant_id, "g": GENESIS_HASH},
    )
    head = (
        await session.execute(
            text(
                "SELECT last_seq, last_hash, last_anchored_seq, anchor_due FROM custody_chain_heads"
                " WHERE stream_id = :s FOR UPDATE"
            ),
            {"s": stream_id},
        )
    ).one()
    seq = head.last_seq + 1
    fields = hashed_fields(
        tenant_id=tenant_id,
        stream_id=stream_id,
        job_id=job_id,
        seq=seq,
        event_type=event_type,
        actor=actor,
        item_id=item_id,
        payload=payload,
        created_at=created,
    )
    event_hash = compute_event_hash(head.last_hash, fields)
    event_id = new_id()
    await session.execute(
        text(
            "INSERT INTO custody_events (id, tenant_id, stream_id, job_id, seq, event_type, actor, item_id,"
            " payload, prev_hash, event_hash, created_at) VALUES (:id, :t, :s, :j, :seq, :et, :actor, :item,"
            " CAST(:payload AS jsonb), :prev, :hash, :created)"
        ),
        {
            "id": event_id,
            "t": tenant_id,
            "s": stream_id,
            "j": job_id,
            "seq": seq,
            "et": event_type,
            "actor": actor,
            "item": item_id,
            "payload": _json(fields["payload"]),
            "prev": head.last_hash,
            "hash": event_hash,
            "created": created,
        },
    )
    due = bool(
        head.anchor_due
        or event_type in LIFECYCLE_EVENTS
        or seq - head.last_anchored_seq >= anchor_every
    )
    await session.execute(
        text(
            "UPDATE custody_chain_heads SET last_seq = :seq, last_hash = :h, anchor_due = :due,"
            " updated_at = now() WHERE stream_id = :s"
        ),
        {"seq": seq, "h": event_hash, "due": due, "s": stream_id},
    )
    return AppendedEvent(event_id, stream_id, seq, event_hash, due)


async def append_batch(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    unit_key: str,
    page_evidence_id: uuid.UUID,
    page_sha256: str,
    items: Sequence[tuple[str, str]],
    actor: str,
    anchor_every: int = 8,
) -> AppendedEvent:
    """One ``items_collected`` event per committed batch, Merkle root over (idempotency_key, content_hash).

    The caller must link every item of the batch in ``job_items`` with this event's id, in the same
    transaction; ``verify_chain`` recomputes the root from exactly those links.
    """
    return await append(
        session,
        tenant_id=tenant_id,
        stream_id=job_id,
        job_id=job_id,
        event_type=BATCH_EVENT,
        actor=actor,
        payload={
            "unit_key": unit_key,
            "page_evidence_id": str(page_evidence_id),
            "page_sha256": page_sha256,
            "item_count": len(items),
            "merkle_root": batch_root(items),
        },
        anchor_every=anchor_every,
    )


# ------------------------------------------------------------------ anchoring
async def _anchor_retain_until(
    session: AsyncSession, settings: Settings, stream_id: uuid.UUID
) -> tuple[datetime, uuid.UUID | None]:
    row = (
        await session.execute(
            text(
                "SELECT j.id AS job_id, m.retention_until FROM collection_jobs j"
                " JOIN matters m ON m.tenant_id = j.tenant_id AND m.id = j.matter_id WHERE j.id = :s"
            ),
            {"s": stream_id},
        )
    ).first()
    if row is not None:
        return effective_retain_until(settings, row.retention_until), row.job_id
    requested = utc_now() + timedelta(days=settings.custody_tenant_anchor_retention_days)
    return effective_retain_until(settings, requested), None


async def anchor_if_due(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    stream_id: uuid.UUID,
    force: bool = False,
) -> str | None:
    """Seal the current head to WORM if due (or ``force``). Returns the anchor key, or None."""
    async with tenant_tx(sessions, tenant_id) as session:
        head = (
            await session.execute(
                text(
                    "SELECT last_seq, last_hash, last_anchored_seq, anchor_due FROM custody_chain_heads"
                    " WHERE stream_id = :s"
                ),
                {"s": stream_id},
            )
        ).first()
        if head is None or head.last_seq == 0:
            return None
        if not (head.anchor_due or force):
            return None
        retain_until, job_id = await _anchor_retain_until(session, settings, stream_id)
        key = anchor_key(str(tenant_id), str(stream_id), head.last_seq)
        await session.execute(
            text(
                "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until)"
                " VALUES (:id, :t, :j, :k, 'anchor', :r) ON CONFLICT (storage_key) DO NOTHING"
            ),
            {"id": new_id(), "t": tenant_id, "j": job_id, "k": key, "r": retain_until},
        )
    body = anchor_document(
        tenant_id=str(tenant_id),
        stream_id=str(stream_id),
        seq=head.last_seq,
        event_hash=head.last_hash,
    )
    stored = await put_immutable(
        s3, bucket=settings.s3_evidence_bucket, key=key, body=body, retain_until=retain_until
    )
    async with tenant_tx(sessions, tenant_id) as session:
        await session.execute(
            text(
                "UPDATE evidence_objects SET state = 'complete', sha256 = :h, size_bytes = :n,"
                " completed_at = now() WHERE storage_key = :k AND state = 'pending'"
            ),
            {"h": stored.sha256, "n": stored.size, "k": key},
        )
        await session.execute(
            text(
                "UPDATE custody_chain_heads SET last_anchored_seq = GREATEST(last_anchored_seq, :seq),"
                " anchor_due = CASE WHEN last_seq = :seq THEN false ELSE anchor_due END"
                " WHERE stream_id = :s"
            ),
            {"seq": head.last_seq, "s": stream_id},
        )
    return key


async def seal_job_chain(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
) -> str:
    """Final seal at job end: anchor the head unconditionally and record the key on the job."""
    key = await anchor_if_due(
        sessions, s3, settings, tenant_id=tenant_id, stream_id=job_id, force=True
    )
    if key is None:
        raise RuntimeError(f"job {job_id} has no custody events to seal")
    async with tenant_tx(sessions, tenant_id) as session:
        await session.execute(
            text("UPDATE collection_jobs SET seal_storage_key = :k WHERE id = :j"),
            {"k": key, "j": job_id},
        )
    return key


# ------------------------------------------------------------------ verification
async def load_anchors(
    s3: S3Client, settings: Settings, verifier: ChainVerifier, *, tenant_id: str, stream_id: str
) -> list[Anchor]:
    anchors: list[Anchor] = []
    async for version in list_versions(
        s3, bucket=settings.s3_evidence_bucket, prefix=anchor_prefix(tenant_id, stream_id)
    ):
        if version.is_delete_marker:
            verifier.add_hidden_anchor(version.key, version.version_id)
            continue
        body = await get_bytes(
            s3, bucket=settings.s3_evidence_bucket, key=version.key, version_id=version.version_id
        )
        anchor = Anchor(version.key, version.version_id, body)
        verifier.add_anchor(anchor)
        anchors.append(anchor)
    return anchors


def event_record_from_row(row: Any) -> EventRecord:
    fields = hashed_fields(
        tenant_id=row.tenant_id,
        stream_id=row.stream_id,
        job_id=row.job_id,
        seq=row.seq,
        event_type=row.event_type,
        actor=row.actor,
        item_id=row.item_id,
        payload=row.payload,
        created_at=row.created_at,
    )
    return EventRecord(str(row.id), fields, row.prev_hash, row.event_hash)


async def verify_chain(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    stream_id: uuid.UUID,
    require_seal: bool | None = None,
    page_size: int = 500,
) -> VerificationReport:
    """Full verification of one stream. ``require_seal`` defaults to "the job has finished"."""
    verifier = ChainVerifier(str(tenant_id), str(stream_id))
    await load_anchors(s3, settings, verifier, tenant_id=str(tenant_id), stream_id=str(stream_id))

    async with tenant_tx(sessions, tenant_id) as session:
        head = (
            await session.execute(
                text("SELECT last_seq, last_hash FROM custody_chain_heads WHERE stream_id = :s"),
                {"s": stream_id},
            )
        ).first()
        finished = (
            await session.execute(
                text("SELECT finished_at IS NOT NULL FROM collection_jobs WHERE id = :s"),
                {"s": stream_id},
            )
        ).scalar()
        after = 0
        while True:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, tenant_id, stream_id, job_id, seq, event_type, actor, item_id, payload,"
                        " prev_hash, event_hash, created_at FROM custody_events"
                        " WHERE stream_id = :s AND seq > :after ORDER BY seq LIMIT :n"
                    ),
                    {"s": stream_id, "after": after, "n": page_size},
                )
            ).all()
            if not rows:
                break
            batch_ids = [r.id for r in rows if r.event_type == BATCH_EVENT]
            items: dict[uuid.UUID, list[tuple[str, str]]] = {i: [] for i in batch_ids}
            if batch_ids:
                linked = await session.execute(
                    text(
                        "SELECT ji.custody_event_id, i.idempotency_key, i.content_hash FROM job_items ji"
                        " JOIN items i ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id"
                        " WHERE ji.custody_event_id = ANY(:ids)"
                    ),
                    {"ids": batch_ids},
                )
                for link in linked:
                    items[link.custody_event_id].append((link.idempotency_key, link.content_hash))
            for row in rows:
                verifier.add_event(event_record_from_row(row), items.get(row.id))
            after = rows[-1].seq
    expected_head = (head.last_seq, head.last_hash) if head is not None else None
    seal = bool(finished) if require_seal is None else require_seal
    return verifier.finish(require_seal=seal, expected_head=expected_head)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
