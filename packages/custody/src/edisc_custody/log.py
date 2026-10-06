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

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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
from edisc_custody.render_files import RENDER_BATCH_EVENT
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
    event_id: uuid.UUID | None = None,
    render_id: uuid.UUID | None = None,
) -> AppendedEvent:
    """Append one event to ``stream_id`` inside the caller's (tenant-scoped) transaction.

    ``render_id``: set on every event of a render's own stream (stream id = render id, job_id NULL;
    a database check enforces both)."""
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
    event_id = event_id or new_id()
    await session.execute(
        text(
            "INSERT INTO custody_events (id, tenant_id, stream_id, job_id, seq, event_type, actor, item_id,"
            " payload, prev_hash, event_hash, created_at, render_id) VALUES (:id, :t, :s, :j, :seq, :et,"
            " :actor, :item, CAST(:payload AS jsonb), :prev, :hash, :created, :render)"
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
            "render": render_id,
        },
    )
    due = bool(
        head.anchor_due
        or event_type in LIFECYCLE_EVENTS
        or seq - head.last_anchored_seq >= anchor_every
    )
    lifecycle = event_type in LIFECYCLE_EVENTS
    await session.execute(
        text(
            "UPDATE custody_chain_heads SET last_seq = :seq, last_hash = :h, anchor_due = :due,"
            " pending_lifecycle_seq = CASE WHEN :lc THEN :seq ELSE pending_lifecycle_seq END,"
            " updated_at = now() WHERE stream_id = :s"
        ),
        {"seq": seq, "h": event_hash, "due": due, "lc": lifecycle, "s": stream_id},
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
    event_id: uuid.UUID | None = None,
) -> AppendedEvent:
    """One ``items_collected`` event per committed batch, Merkle root over (idempotency_key, content_hash).

    ``items`` must be exactly the job links created with this event's id in the same transaction;
    ``verify_chain`` recomputes the root from those links. Pass the pre-allocated ``event_id`` when the
    links were inserted first (the FK to custody_events is deferred).
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
        event_id=event_id,
    )


# ------------------------------------------------------------------ anchoring
async def _anchor_retain_until(
    session: AsyncSession, settings: Settings, stream_id: uuid.UUID
) -> tuple[datetime, uuid.UUID | None, uuid.UUID | None]:
    """(retain until, job id, render id) for an anchor of ``stream_id``: a job stream belongs to its
    job's matter; a render stream to its render, whose job's matter owns the retention (render ->
    job -> matter); the tenant stream has the rolling window only."""
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
        return effective_retain_until(settings, row.retention_until), row.job_id, None
    render = (
        await session.execute(
            text(
                "SELECT r.id, m.retention_until FROM renders r"
                " JOIN collection_jobs j ON j.tenant_id = r.tenant_id AND j.id = r.job_id"
                " JOIN matters m ON m.tenant_id = j.tenant_id AND m.id = j.matter_id WHERE r.id = :s"
            ),
            {"s": stream_id},
        )
    ).first()
    if render is not None:
        return effective_retain_until(settings, render.retention_until), None, render.id
    return effective_retain_until(settings), None, None  # tenant stream: rolling window only


async def anchor_if_due(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    stream_id: uuid.UUID,
    force: bool = False,
    wait: bool = True,
) -> str | None:
    """Seal the current head to WORM if due (or ``force``). A forced call that finds a live claim waits
    for it when ``wait`` (seal), else skips (sweeper: the live claimer finishes the job). Returns the newest anchor key written (or,
    when forced and the head is already anchored, that anchor's key), else None.

    Coalesced: a writer must win an atomic claim on the head to anchor; concurrent writers skip instead
    of each writing an anchor (the "anchor storm"). The claimer keeps anchoring while the stream stays
    due (bounded), so a burst ends with its last due point anchored. A claim older than
    ``custody_anchor_claim_timeout_seconds`` is abandoned and taken over (the anchor sweeper covers a
    claimer that was killed mid-anchor)."""
    newest: str | None = None
    # a non-forced call drains every due point (a burst's writers skip while one holds the claim);
    # each round advances the anchored seq, so this ends; the cap only guards against a stuck head
    for _ in range(1000 if not force else 1):
        key = await _anchor_once(
            sessions, s3, settings, tenant_id, stream_id, force=force, waiting=not wait
        )
        if key is None:
            break
        newest = key
    return newest


async def _claim(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    tenant_id: uuid.UUID,
    stream_id: uuid.UUID,
    *,
    force: bool,
) -> Any:
    async with tenant_tx(sessions, tenant_id) as session:
        return (
            await session.execute(
                # the claimed seq is the next DUE POINT, not the moving head: the next lifecycle event or
                # the next interval boundary (forced: the head). So a burst gets one anchor per due point.
                text(
                    "UPDATE custody_chain_heads SET anchoring_since = now(), anchoring_seq = CASE"
                    "   WHEN CAST(:force AS boolean) THEN last_seq"
                    "   WHEN pending_lifecycle_seq > last_anchored_seq"
                    "     THEN LEAST(pending_lifecycle_seq, last_anchored_seq + :every, last_seq)"
                    "   ELSE LEAST(last_seq, last_anchored_seq + :every) END"
                    " WHERE stream_id = :s AND last_seq > 0 AND last_seq > last_anchored_seq"
                    " AND (CAST(:force AS boolean) OR anchor_due)"
                    " AND (anchoring_seq IS NULL OR anchoring_since < now() - make_interval(secs => :stale))"
                    " RETURNING anchoring_seq"
                ),
                {
                    "s": stream_id,
                    "force": force,
                    "every": settings.custody_anchor_every_n_batches,
                    "stale": settings.custody_anchor_claim_timeout_seconds,
                },
            )
        ).first()


async def _anchor_once(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    tenant_id: uuid.UUID,
    stream_id: uuid.UUID,
    *,
    force: bool,
    waiting: bool = False,
) -> str | None:
    claimed = await _claim(sessions, settings, tenant_id, stream_id, force=force)
    if claimed is None:
        if not force or waiting:
            return None
        return await _forced_without_claim(sessions, s3, settings, tenant_id, stream_id)
    seq: int = claimed.anchoring_seq
    try:
        async with tenant_tx(sessions, tenant_id) as session:
            head_hash: str = (
                await session.execute(
                    text("SELECT event_hash FROM custody_events WHERE stream_id = :s AND seq = :q"),
                    {"s": stream_id, "q": seq},
                )
            ).scalar_one()
            retain_until, job_id, render_id = await _anchor_retain_until(
                session, settings, stream_id
            )
            key = anchor_key(str(tenant_id), str(stream_id), seq)
            body = anchor_document(
                tenant_id=str(tenant_id), stream_id=str(stream_id), seq=seq, event_hash=head_hash
            )
            # the anchor's hash is known before the object exists: persisted with the row (provenance)
            await session.execute(
                text(
                    "INSERT INTO evidence_objects (id, tenant_id, job_id, render_id, storage_key, kind,"
                    " retain_until, source_sha256, source_hash_origin)"
                    " VALUES (:id, :t, :j, :rid, :k, 'anchor', :r, :h, 'collection')"
                    " ON CONFLICT (storage_key) DO NOTHING"
                ),
                {
                    "id": new_id(),
                    "t": tenant_id,
                    "j": job_id,
                    "rid": render_id,
                    "k": key,
                    "r": retain_until,
                    "h": hashlib.sha256(body).hexdigest(),
                },
            )
        stored = await put_immutable(
            s3, bucket=settings.s3_evidence_bucket, key=key, body=body, retain_until=retain_until
        )
    except Exception:
        # an ordinary failure releases the claim at once (a kill leaves it to the timeout + sweeper)
        async with tenant_tx(sessions, tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE custody_chain_heads SET anchoring_seq = NULL, anchoring_since = NULL"
                    " WHERE stream_id = :s AND anchoring_seq = :seq"
                ),
                {"s": stream_id, "seq": seq},
            )
        raise
    async with tenant_tx(sessions, tenant_id) as session:
        await session.execute(
            text(
                "UPDATE evidence_objects SET state = 'complete', sha256 = :h, size_bytes = :n,"
                " version_id = :v, completed_at = now() WHERE storage_key = :k AND state = 'pending'"
            ),
            {"h": stored.sha256, "n": stored.size, "v": stored.version_id, "k": key},
        )
        # still due afterwards only if the head ran a full interval past this anchor, or a lifecycle
        # event after it is not covered yet; the claim is released only if it is still ours
        await session.execute(
            text(
                "UPDATE custody_chain_heads SET last_anchored_seq = GREATEST(last_anchored_seq, :seq),"
                " anchor_due = (last_seq - GREATEST(last_anchored_seq, :seq) >= :every)"
                "   OR (pending_lifecycle_seq > GREATEST(last_anchored_seq, :seq)),"
                " anchoring_since = CASE WHEN anchoring_seq = :seq THEN NULL ELSE anchoring_since END,"
                " anchoring_seq = CASE WHEN anchoring_seq = :seq THEN NULL ELSE anchoring_seq END"
                " WHERE stream_id = :s"
            ),
            {"seq": seq, "every": settings.custody_anchor_every_n_batches, "s": stream_id},
        )
    return key


async def _forced_without_claim(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    tenant_id: uuid.UUID,
    stream_id: uuid.UUID,
) -> str | None:
    """A forced anchor (seal, sweeper) that could not claim: either the head is already anchored (return
    that anchor), or another writer is anchoring; wait for it (bounded by the claim timeout) and retry."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.custody_anchor_claim_timeout_seconds + 5
    while True:
        async with tenant_tx(sessions, tenant_id) as session:
            head = (
                await session.execute(
                    text(
                        "SELECT last_seq, last_anchored_seq, anchoring_seq FROM custody_chain_heads"
                        " WHERE stream_id = :s"
                    ),
                    {"s": stream_id},
                )
            ).first()
        if head is None or head.last_seq == 0:
            return None
        if head.last_anchored_seq == head.last_seq:
            return anchor_key(str(tenant_id), str(stream_id), head.last_seq)
        if loop.time() > deadline:
            raise TimeoutError(f"stream {stream_id}: anchoring claim held past its timeout")
        await asyncio.sleep(0.1)
        key = await _anchor_once(
            sessions, s3, settings, tenant_id, stream_id, force=True, waiting=True
        )
        if key is not None:
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


def _verify_page(
    verifier: ChainVerifier,
    rows: Sequence[Any],
    batch_ids: Sequence[uuid.UUID],
    linked: Sequence[Any],
    files: Mapping[uuid.UUID, list[dict[str, Any]]],
    natives: Mapping[uuid.UUID, list[dict[str, Any]]],
) -> None:
    items: dict[uuid.UUID, list[tuple[str, str]]] = {i: [] for i in batch_ids}
    for link in linked:
        items[link.custody_event_id].append((link.idempotency_key, link.content_hash))
    for row in rows:
        verifier.add_event(
            event_record_from_row(row), items.get(row.id), files.get(row.id), natives.get(row.id)
        )


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
    """Full verification of one stream (a job's, a render's or the tenant's). ``require_seal``
    defaults to "the job (or render) has finished"."""
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
                text(
                    "SELECT coalesce((SELECT finished_at IS NOT NULL FROM collection_jobs WHERE id = :s),"
                    " (SELECT finished_at IS NOT NULL FROM renders WHERE id = :s))"
                ),
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
            linked: Sequence[Any] = ()
            if batch_ids:
                linked = (
                    await session.execute(
                        text(
                            "SELECT ji.custody_event_id, i.idempotency_key, i.content_hash FROM job_items ji"
                            " JOIN items i ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id"
                            " WHERE ji.custody_event_id = ANY(:ids)"
                        ),
                        {"ids": batch_ids},
                    )
                ).all()
            file_batches = [r.id for r in rows if r.event_type == RENDER_BATCH_EVENT]
            files: dict[uuid.UUID, list[dict[str, Any]]] = {i: [] for i in file_batches}
            if file_batches:
                stored = await session.execute(
                    text(
                        "SELECT custody_event_id, ord, name, version_id, sha256, size_bytes, record"
                        " FROM render_files WHERE custody_event_id = ANY(:ids) ORDER BY ord"
                    ),
                    {"ids": file_batches},
                )
                for f in stored:
                    rec = dict(f.record)
                    columns = (f.ord, f.name, f.version_id, f.sha256, f.size_bytes)
                    recorded = tuple(
                        rec.get(k) for k in ("ord", "name", "version_id", "sha256", "size")
                    )
                    if columns != recorded:
                        verifier.report.fail(
                            f"render file {f.ord}: columns {columns} disagree with its record {recorded}"
                        )
                    files[f.custody_event_id].append(rec)
            natives: dict[uuid.UUID, list[dict[str, Any]]] = {i: [] for i in file_batches}
            if file_batches:
                native_rows = await session.execute(
                    text(
                        "SELECT custody_event_id, ord, sha256, size_bytes, storage_key, version_id,"
                        " file_ords FROM render_natives WHERE custody_event_id = ANY(:ids) ORDER BY ord"
                    ),
                    {"ids": file_batches},
                )
                for n in native_rows:
                    natives[n.custody_event_id].append(
                        {"ord": n.ord, "sha256": n.sha256, "size": n.size_bytes,
                         "storage_key": n.storage_key, "version_id": n.version_id,
                         "file_ords": list(n.file_ords)}
                    )  # fmt: skip
            # hashing every event and recomputing every batch's Merkle root is CPU work: in a
            # thread, one page at a time (the verifier is only ever touched by one thread at once)
            await asyncio.to_thread(_verify_page, verifier, rows, batch_ids, linked, files, natives)
            after = rows[-1].seq
    expected_head = (head.last_seq, head.last_hash) if head is not None else None
    seal = bool(finished) if require_seal is None else require_seal
    return verifier.finish(require_seal=seal, expected_head=expected_head)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
