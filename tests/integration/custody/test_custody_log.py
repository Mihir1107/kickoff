"""M5: custody chain, batch Merkle roots, WORM anchors, and tampering by a superuser."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_custody.chain import ChainVerifier
from edisc_custody.log import anchor_if_due, append, event_record_from_row, verify_chain
from edisc_db.session import tenant_tx

from .conftest import ANCHOR_EVERY, Job, new_job, rewrite_chain_from, run_job, superuser

Sessions = async_sessionmaker[AsyncSession]


async def _verify(sessions: Sessions, s3: S3Client, settings: Settings, job: Job):  # type: ignore[no-untyped-def]
    return await verify_chain(sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id)


async def test_clean_job_verifies_with_periodic_and_final_anchors(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await run_job(app_sessions, s3, settings, batches=20, items_per_batch=5)
    report = await _verify(app_sessions, s3, settings, job)
    assert report.ok, report.errors
    assert report.events == 22  # job_started + 20 batches + job_finished
    assert report.batches_checked == 20
    assert report.items_checked == 100
    # job_started (seq 1), then every ANCHOR_EVERY events (9, 17), job_finished (22) = seal
    assert report.anchors_checked == 4
    assert [int(k.rsplit("/", 1)[1].split(".")[0]) for k in sorted(set(job.anchors))] == [
        1,
        1 + ANCHOR_EVERY,
        1 + 2 * ANCHOR_EVERY,
        22,
    ]


async def test_concurrent_writers_get_a_gapless_chain(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await new_job(app_sessions)

    async def writer(w: int) -> None:
        for i in range(15):
            async with tenant_tx(app_sessions, job.tenant_id) as s:
                await append(
                    s,
                    tenant_id=job.tenant_id,
                    stream_id=job.job_id,
                    job_id=job.job_id,
                    event_type="note",
                    actor=f"w{w}",
                    payload={"i": i},
                )

    await asyncio.gather(*(writer(w) for w in range(30)))
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        seqs = (
            (
                await s.execute(
                    text("SELECT seq FROM custody_events WHERE stream_id = :j ORDER BY seq"),
                    {"j": job.job_id},
                )
            )
            .scalars()
            .all()
        )
    assert seqs == list(range(1, 451))
    await anchor_if_due(
        app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id, force=True
    )
    report = await verify_chain(
        app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id, require_seal=True
    )
    assert report.ok, report.errors


async def test_anchor_due_flag_survives_a_crash_before_anchoring(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await new_job(app_sessions)
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        ev = await append(
            s,
            tenant_id=job.tenant_id,
            stream_id=job.job_id,
            job_id=job.job_id,
            event_type="job_started",
            actor="t",
            payload={},
        )
    assert ev.anchor_due
    # crash here: no anchor written. A later, non-lifecycle append must keep the flag set.
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        later = await append(
            s,
            tenant_id=job.tenant_id,
            stream_id=job.job_id,
            job_id=job.job_id,
            event_type="note",
            actor="t",
            payload={},
        )
    assert later.anchor_due
    key = await anchor_if_due(
        app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id
    )
    assert key is not None
    # anchored at its due point, the lifecycle event (seq 1); seq 2 is within the anchoring interval
    assert key.endswith("0000000000000001.json")
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT last_anchored_seq, anchor_due FROM custody_chain_heads WHERE stream_id = :j"
                ),
                {"j": job.job_id},
            )
        ).one()
    assert (row.last_anchored_seq, row.anchor_due) == (1, False)
    # a forced anchor (seal) covers the head; forcing again is an idempotent no-op on WORM
    head_key = await anchor_if_due(
        app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id, force=True
    )
    assert head_key is not None and head_key.endswith("0000000000000002.json")
    assert (
        await anchor_if_due(
            app_sessions, s3, settings, tenant_id=job.tenant_id, stream_id=job.job_id, force=True
        )
        == head_key
    )


async def test_payload_rejects_values_that_do_not_roundtrip(app_sessions: Sessions) -> None:
    job = await new_job(app_sessions)
    for bad in ({"x": 1.5}, {"x": "a\x00b"}):
        with pytest.raises(ValueError, match="not allowed"):
            async with tenant_tx(app_sessions, job.tenant_id) as s:
                await append(
                    s,
                    tenant_id=job.tenant_id,
                    stream_id=job.job_id,
                    job_id=job.job_id,
                    event_type="note",
                    actor="t",
                    payload=bad,
                )


# ------------------------------------------------------------------ superuser tampering
async def test_edit_without_rehash_is_detected(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await run_job(app_sessions, s3, settings, batches=4)
    su = await superuser(settings)
    try:
        await su.execute(
            "UPDATE custody_events SET actor = 'mallory' WHERE stream_id = $1 AND seq = 3",
            job.job_id,
        )
    finally:
        await su.close()
    report = await _verify(app_sessions, s3, settings, job)
    assert any("seq 3: event_hash mismatch" in e for e in report.errors), report.errors


async def test_internally_consistent_rewrite_is_caught_only_by_worm_anchors(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    """The required scenario: a superuser rewrites an event AND recomputes every later hash and the head.
    The DB chain is internally valid again; only the WORM anchors disagree."""
    job = await run_job(app_sessions, s3, settings, batches=20)
    su = await superuser(settings)
    try:
        await rewrite_chain_from(su, job.job_id, 3, {"note": "forged"})
        # the forged chain on its own is internally consistent
        rows = await su.fetch(
            "SELECT * FROM custody_events WHERE stream_id = $1 ORDER BY seq", job.job_id
        )
    finally:
        await su.close()

    class _Row:
        def __init__(self, r: object) -> None:
            self.__dict__.update(dict(r))  # type: ignore[call-overload]
            self.payload = json.loads(self.payload)

    internal = ChainVerifier(str(job.tenant_id), str(job.job_id))
    for r in rows:
        rec = event_record_from_row(_Row(r))
        internal.add_event(rec, None if rec.event_type != "items_collected" else [])
    internal_errors = [
        e
        for e in internal.finish(require_seal=False).errors
        if "Merkle" not in e and "item_count" not in e
    ]
    assert internal_errors == [], "forged chain should be internally consistent"

    report = await _verify(app_sessions, s3, settings, job)
    assert not report.ok
    assert any("disagrees with the WORM anchor (chain rewritten)" in e for e in report.errors), (
        report.errors
    )


async def test_item_row_tampering_breaks_the_batch_merkle_root(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await run_job(app_sessions, s3, settings, batches=3)
    su = await superuser(settings)
    try:
        await su.execute(
            "UPDATE items SET content_hash = $2 WHERE id = (SELECT item_id FROM job_items WHERE job_id = $1 LIMIT 1)",
            job.job_id,
            "ee" * 32,
        )
    finally:
        await su.close()
    report = await _verify(app_sessions, s3, settings, job)
    assert any("Merkle root mismatch" in e for e in report.errors), report.errors


async def test_removing_an_item_link_is_detected(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await run_job(app_sessions, s3, settings, batches=3)
    su = await superuser(settings)
    try:
        await su.execute(
            "DELETE FROM job_items WHERE item_id = (SELECT item_id FROM job_items WHERE job_id = $1 LIMIT 1)",
            job.job_id,
        )
    finally:
        await su.close()
    report = await _verify(app_sessions, s3, settings, job)
    assert any("item_count" in e for e in report.errors), report.errors


async def test_tail_truncation_is_detected_by_anchors(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await run_job(app_sessions, s3, settings, batches=20)
    su = await superuser(settings)
    try:
        await su.execute(
            "DELETE FROM job_items WHERE job_id = $1 AND custody_event_id IN (SELECT id FROM custody_events WHERE stream_id = $1 AND seq > 19)",
            job.job_id,
        )
        await su.execute("DELETE FROM custody_events WHERE stream_id = $1 AND seq > 19", job.job_id)
        h = await su.fetchval(
            "SELECT event_hash FROM custody_events WHERE stream_id = $1 AND seq = 19", job.job_id
        )
        await su.execute(
            "UPDATE custody_chain_heads SET last_seq = 19, last_hash = $2, last_anchored_seq = 17 WHERE stream_id = $1",
            job.job_id,
            h,
        )
    finally:
        await su.close()
    report = await _verify(app_sessions, s3, settings, job)
    assert any("the chain ends at 19 (events deleted)" in e for e in report.errors), report.errors


async def test_finished_job_without_seal_fails(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await run_job(app_sessions, s3, settings, batches=2, finalize=False)
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        await append(
            s,
            tenant_id=job.tenant_id,
            stream_id=job.job_id,
            job_id=job.job_id,
            event_type="note",
            actor="t",
            payload={},
        )
        await s.execute(
            text("UPDATE collection_jobs SET finished_at = now() WHERE id = :j"), {"j": job.job_id}
        )
    report = await _verify(app_sessions, s3, settings, job)
    assert any("no matching WORM seal" in e for e in report.errors), report.errors


async def test_hiding_an_anchor_with_a_delete_marker_is_detected(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await run_job(app_sessions, s3, settings, batches=2)
    # S3 root can add a delete marker (Object Lock allows it; the locked version survives)
    await s3.delete_object(Bucket=settings.s3_evidence_bucket, Key=sorted(set(job.anchors))[0])
    report = await _verify(app_sessions, s3, settings, job)
    assert any("delete marker" in e for e in report.errors), report.errors


async def test_shadowing_an_anchor_with_a_newer_version_is_detected(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    job = await run_job(app_sessions, s3, settings, batches=2)
    key = sorted(set(job.anchors))[0]
    original = json.loads(
        await (await s3.get_object(Bucket=settings.s3_evidence_bucket, Key=key))["Body"].read()
    )
    forged = canonical_json({**original, "event_hash": "0" * 64})
    await s3.put_object(
        Bucket=settings.s3_evidence_bucket, Key=key, Body=forged
    )  # no If-None-Match: new version
    report = await _verify(app_sessions, s3, settings, job)
    # both versions are checked: the original (locked) agrees, the shadow disagrees
    assert (
        sum(1 for e in report.errors if key in e and "disagrees with the WORM anchor" in e) == 1
    ), report.errors
    assert report.anchors_checked == len(set(job.anchors)) + 1
