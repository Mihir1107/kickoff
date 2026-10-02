"""Coalesced anchoring (anchor-storm fix): one writer anchors per due point, nothing is left behind."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import anchor_if_due, append, verify_chain
from edisc_custody.sweeper import sweep_anchors
from edisc_db.session import tenant_tx

from .conftest import Job, new_job

Sessions = async_sessionmaker[AsyncSession]
WRITERS = 200


async def _head(sessions: Sessions, job: Job) -> Any:
    async with tenant_tx(sessions, job.tenant_id) as s:
        return (
            await s.execute(
                text("SELECT * FROM custody_chain_heads WHERE stream_id = :s"), {"s": job.job_id}
            )
        ).one()


async def _anchor_seqs(sessions: Sessions, job: Job) -> list[int]:
    async with tenant_tx(sessions, job.tenant_id) as s:
        keys = (
            await s.execute(
                text(
                    "SELECT storage_key FROM evidence_objects WHERE kind = 'anchor' AND storage_key LIKE :p"
                ),
                {"p": f"custody-anchors/{job.tenant_id}/{job.job_id}/%"},
            )
        ).scalars()
        return sorted(int(k.rsplit("/", 1)[1].split(".")[0]) for k in keys)


async def test_concurrent_writers_produce_one_anchor_per_due_point(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    every = settings.custody_anchor_every_n_batches
    job = await new_job(app_sessions)
    lifecycle_seq: list[int] = []

    async def writer(i: int) -> None:
        lifecycle = i == WRITERS // 2
        async with tenant_tx(app_sessions, job.tenant_id) as s:
            event = await append(
                s,
                tenant_id=job.tenant_id,
                stream_id=job.job_id,
                job_id=job.job_id,
                event_type="evidence_verified" if lifecycle else "note",
                actor=f"writer-{i}",
                payload={"i": i},
                anchor_every=every,
            )
        if lifecycle:
            lifecycle_seq.append(event.seq)
        await anchor_if_due(
            app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id
        )

    await asyncio.gather(*(writer(i) for i in range(WRITERS)))

    anchors = await _anchor_seqs(app_sessions, job)
    head = await _head(app_sessions, job)
    intended = math.ceil(WRITERS / every) + 1  # one per interval, plus the lifecycle event
    assert 1 <= len(anchors) <= intended + 2, (
        f"{len(anchors)} anchors for {WRITERS} events (intended ~{intended})"
    )
    assert head.last_seq == WRITERS
    assert head.anchoring_seq is None  # no claim left behind
    assert head.last_seq - head.last_anchored_seq < every  # no event unanchored beyond the gap
    assert max(anchors) >= lifecycle_seq[0]  # the lifecycle event is covered by an anchor
    # one anchor per due point: interval boundaries and the lifecycle event, never the moving head
    gaps = [b - a for a, b in zip([0, *anchors], anchors, strict=False)]
    assert max(gaps) <= every, f"anchors {anchors}"
    assert lifecycle_seq[0] in anchors
    report = await verify_chain(
        app_sessions,
        s3,
        settings,
        tenant_id=job.tenant_id,
        stream_id=job.job_id,
        require_seal=False,
    )
    assert report.ok, report.errors


async def test_a_claimer_killed_mid_anchor_is_covered_by_the_sweeper(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await new_job(app_sessions)
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        for i in range(3):
            await append(
                s, tenant_id=job.tenant_id, stream_id=job.job_id, job_id=job.job_id,
                event_type="note", actor="t", payload={"i": i}, anchor_every=2,
            )  # fmt: skip
        # a writer claimed the anchor and was SIGKILLed before writing it
        await s.execute(
            text(
                "UPDATE custody_chain_heads SET anchoring_seq = last_seq, anchoring_since = now()"
                " WHERE stream_id = :s"
            ),
            {"s": job.job_id},
        )
    # while the claim is fresh, nobody else anchors (no duplicate work, no waiting in the sweeper)
    fresh = await sweep_anchors(
        sweeper_sessions, app_sessions, s3, settings, tenant_id=job.tenant_id
    )
    assert fresh.anchored == [] and await _anchor_seqs(app_sessions, job) == []
    assert (
        await anchor_if_due(
            app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id
        )
        is None
    )
    # once the claim is older than the timeout it is abandoned: the sweeper takes it over
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        await s.execute(
            text(
                "UPDATE custody_chain_heads SET anchoring_since = now() - make_interval(secs => :age)"
                " WHERE stream_id = :s"
            ),
            {"s": job.job_id, "age": settings.custody_anchor_claim_timeout_seconds + 1},
        )
    swept = await sweep_anchors(
        sweeper_sessions, app_sessions, s3, settings, tenant_id=job.tenant_id
    )
    assert len(swept.anchored) == 1
    head = await _head(app_sessions, job)
    assert (head.last_anchored_seq, head.anchoring_seq, head.anchor_due) == (3, None, False)


async def test_an_ordinary_failure_releases_the_claim_at_once(
    app_sessions: Sessions, s3: S3Client, settings: Settings, monkeypatch: Any
) -> None:
    import edisc_custody.log as log

    job = await new_job(app_sessions)
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        await append(
            s, tenant_id=job.tenant_id, stream_id=job.job_id, job_id=job.job_id,
            event_type="evidence_verified", actor="t", payload={},
        )  # fmt: skip

    async def broken(*_: Any, **__: Any) -> Any:
        raise ConnectionError("S3 unavailable")

    monkeypatch.setattr(log, "put_immutable", broken)
    with pytest.raises(ConnectionError):  # the failure surfaces
        await anchor_if_due(
            app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id
        )
    assert (await _head(app_sessions, job)).anchoring_seq is None
    monkeypatch.undo()
    key = await anchor_if_due(
        app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id
    )
    assert key is not None
