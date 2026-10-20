"""Legal holds below the API (M18 §1.1-§1.3, §7; D2; threats T5-adjacent, T16): the one "held"
answer through every owner, retention under hold, and the S3 flags (the holds worker as the hold
identity) with their crash points, overlapping holds and a flag removed behind our back.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now
from edisc_custody import holds
from edisc_custody.log import append_batch
from edisc_custody.retention_extension import extend_retention
from edisc_db.session import tenant_tx
from edisc_evidence.s3 import s3_client
from edisc_worker.holds import HoldFlagger

from .conftest import run_job
from .test_retention_extension import _export, window_settings

Sessions = async_sessionmaker[AsyncSession]
DAY = timedelta(days=1)


@pytest.fixture(scope="module")
async def hold_s3(settings: Settings) -> AsyncIterator[S3Client]:
    async with s3_client(settings, "hold") as client:
        yield client


async def _setup(app_sessions: Sessions, s3: S3Client, settings: Settings) -> dict[str, Any]:
    """Matter C owns job c (pages); matter A's job a links one of c's pages (dedup across matters)."""
    c = await run_job(app_sessions, s3, settings, batches=3, items_per_batch=2)
    tenant = c.tenant_id
    a_matter, a_job = new_id(), new_id()
    async with tenant_tx(app_sessions, tenant) as s:
        c_matter = (await s.execute(text("SELECT matter_id FROM collection_jobs WHERE id = :j"),
                                    {"j": c.job_id})).scalar_one()  # fmt: skip
        client = (await s.execute(text("SELECT client_id FROM matters WHERE id = :m"),
                                  {"m": c_matter})).scalar_one()  # fmt: skip
        conn = (await s.execute(text("SELECT id FROM connections LIMIT 1"))).scalar_one()
        await s.execute(text(
            "INSERT INTO matters (id, tenant_id, client_id, name, retention_until)"
            " VALUES (:m, :t, :c, 'A', now() + interval '200 days')"),
            {"m": a_matter, "t": tenant, "c": client})  # fmt: skip
        await s.execute(text(
            "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status,"
            " connector_version, requested_by) VALUES (:j, :t, :m, :c, 'running', '0.1.0', 't')"),
            {"j": a_job, "t": tenant, "m": a_matter, "c": conn})  # fmt: skip
        pages = list((await s.execute(text(
            "SELECT id FROM evidence_objects WHERE job_id = :j AND state = 'complete' ORDER BY id"),
            {"j": c.job_id})).scalars())  # fmt: skip
        shared = (await s.execute(text(
            "SELECT evidence_object_id FROM items WHERE job_id = :j ORDER BY evidence_object_id LIMIT 1"),
            {"j": c.job_id})).scalar_one()  # fmt: skip
        items = (await s.execute(text(
            "SELECT id, idempotency_key, content_hash FROM items WHERE evidence_object_id = :e"),
            {"e": shared})).all()  # fmt: skip
        await s.execute(text(
            "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day)"
            " VALUES (:t, :j, 'C1/2026-01-01', 'C1', '2026-01-01')"), {"t": tenant, "j": a_job})  # fmt: skip
        sha = (await s.execute(text("SELECT sha256 FROM evidence_objects WHERE id = :e"),
                               {"e": shared})).scalar_one()  # fmt: skip
        event = await append_batch(
            s, tenant_id=tenant, job_id=a_job, unit_key="C1/2026-01-01", page_evidence_id=shared,
            page_sha256=sha, items=[(r.idempotency_key, r.content_hash) for r in items], actor="tests",
        )  # fmt: skip
        for r in items:
            await s.execute(text(
                "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id)"
                " VALUES (:t, :j, :i, 'C1/2026-01-01', :e)"),
                {"t": tenant, "j": a_job, "i": r.id, "e": event.id})  # fmt: skip
    return {"tenant": tenant, "client": client, "c_matter": c_matter, "a_matter": a_matter,
            "c_job": c.job_id, "a_job": a_job, "pages": pages, "shared": shared}  # fmt: skip


async def _hold(
    sessions: Sessions, tenant: uuid.UUID, *, matter: Any = None, client: Any = None
) -> uuid.UUID:
    hold = new_id()
    async with tenant_tx(sessions, tenant) as s:
        await s.execute(text(
            "INSERT INTO legal_holds (id, tenant_id, matter_id, client_id, reason, placed_by)"
            " VALUES (:i, :t, :m, :c, 'test', 'tests')"),
            {"i": hold, "t": tenant, "m": matter, "c": client})  # fmt: skip
    return hold


async def _release(sessions: Sessions, tenant: uuid.UUID, hold: uuid.UUID) -> None:
    async with tenant_tx(sessions, tenant) as s:
        await s.execute(text(
            "UPDATE legal_holds SET released_at = now(), released_by = 'tests', release_reason = 'x',"
            " cooling_off_until = now() + interval '7 days', flag_state = 'clearing' WHERE id = :h"),
            {"h": hold})  # fmt: skip


async def _flag_state(s3_root: S3Client, settings: Settings, sessions: Sessions, tenant: uuid.UUID,
                      ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:  # fmt: skip
    async with tenant_tx(sessions, tenant) as s:
        rows = (await s.execute(text(
            "SELECT id, storage_key, version_id FROM evidence_objects WHERE id = ANY(:i)"),
            {"i": ids})).all()  # fmt: skip
    out = {}
    for r in rows:
        try:
            got = await s3_root.get_object_legal_hold(
                Bucket=settings.s3_evidence_bucket, Key=r.storage_key, VersionId=r.version_id
            )
            out[r.id] = got["LegalHold"]["Status"]
        except ClientError:
            out[r.id] = "OFF"
    return out


async def _run(flagger: HoldFlagger, tenant: uuid.UUID, hold: uuid.UUID) -> int:
    state = await flagger.begin(tenant, hold)
    steps = 0
    while state in ("flagging", "clearing") and not await flagger.step(tenant, hold):
        steps += 1
        assert steps < 200
    return steps


async def _row(sessions: Sessions, tenant: uuid.UUID, hold: uuid.UUID) -> Any:
    async with tenant_tx(sessions, tenant) as s:
        return (await s.execute(text("SELECT * FROM legal_holds WHERE id = :h"), {"h": hold})).one()


async def test_held_is_answered_through_every_owner(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    x = await _setup(app_sessions, s3, settings)
    t = x["tenant"]
    export = await _export(app_sessions, s3, settings, t, x["client"])
    a_hold = await _hold(app_sessions, t, matter=x["a_matter"])
    async with tenant_tx(app_sessions, t) as s:
        assert await holds.evidence_held(s, x["shared"])  # A's job linked C's page
        others = [p for p in x["pages"] if p != x["shared"]]
        assert others and not any([await holds.evidence_held(s, p) for p in others])
        assert await holds.matter_held(s, x["a_matter"]) and not await holds.matter_held(
            s, x["c_matter"]
        )
        assert await holds.client_held(s, x["client"])  # a matter of it is held
        assert not await holds.evidence_held(s, export)  # a matter hold never covers the exports
    client_hold = await _hold(app_sessions, t, client=x["client"])
    async with tenant_tx(app_sessions, t) as s:
        assert all([await holds.evidence_held(s, p) for p in x["pages"]])
        assert await holds.evidence_held(s, export)
        assert await holds.matter_held(s, x["c_matter"])
    for h in (a_hold, client_hold):
        await _release(app_sessions, t, h)
    async with tenant_tx(app_sessions, t) as s:
        assert not await holds.evidence_held(s, x["shared"])
        assert not await holds.client_held(s, x["client"])
        until = await holds.cooling_off_until(s, x["shared"])
        assert until is not None and ensure_utc(until) > utc_now() + 6 * DAY


async def test_retention_is_extended_under_hold_whatever_the_matter_or_client_state(
    app_sessions: Sessions, sweeper_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    rs = window_settings(settings)
    x = await _setup(app_sessions, s3, rs)
    t = x["tenant"]
    export = await _export(app_sessions, s3, rs, t, x["client"])
    async with tenant_tx(app_sessions, t) as s:  # both matters, then the client, closed
        for m in (x["c_matter"], x["a_matter"]):
            await s.execute(text(
                "UPDATE matters SET closed_at = now(), closed_by = 'tests' WHERE id = :m"), {"m": m})  # fmt: skip
        await s.execute(text("UPDATE clients SET closed_at = now(), closed_by = 't' WHERE id = :c"),
                        {"c": x["client"]})  # fmt: skip
    later = utc_now() + 80 * DAY  # below the floor: anything owned is due
    res = await extend_retention(sweeper_sessions, app_sessions, s3, rs, tenant_id=t, now=later)
    assert (res.extended["matter"], res.extended["export"]) == (0, 0)  # nothing open, nothing held
    await _hold(app_sessions, t, matter=x["c_matter"])  # a hold on a CLOSED matter
    res = await extend_retention(sweeper_sessions, app_sessions, s3, rs, tenant_id=t, now=later)
    assert res.extended["matter"] == len(x["pages"])  # every object of the held matter's job
    assert res.extended["export"] == 0  # a matter hold never covers the client's exports
    await _hold(app_sessions, t, client=x["client"])
    res = await extend_retention(sweeper_sessions, app_sessions, s3, rs, tenant_id=t, now=later)
    assert res.extended["export"] == 1  # a client held directly: its locked export
    del export


async def test_flags_cover_the_scope_and_report_progress_honestly(
    app_sessions: Sessions, s3: S3Client, s3_root: S3Client, hold_s3: S3Client, settings: Settings
) -> None:
    x = await _setup(app_sessions, s3, settings)
    t = x["tenant"]
    hold = await _hold(app_sessions, t, matter=x["c_matter"])
    before = await _row(app_sessions, t, hold)
    assert (before.flag_state, before.objects_flagged, before.objects_in_scope) == (
        "pending",
        0,
        None,
    )
    flagger = HoldFlagger(app_sessions, s3, hold_s3, settings)
    await _run(flagger, t, hold)
    row = await _row(app_sessions, t, hold)
    assert (row.flag_state, row.objects_flagged, row.objects_in_scope) == (
        "flagged",
        len(x["pages"]),
        len(x["pages"]),
    )
    assert set((await _flag_state(s3_root, settings, app_sessions, t, x["pages"])).values()) == {
        "ON"
    }
    # a flagged version cannot be deleted even by root
    async with tenant_tx(app_sessions, t) as s:
        e = (await s.execute(text("SELECT storage_key, version_id FROM evidence_objects WHERE id = :i"),
                             {"i": x["pages"][0]})).one()  # fmt: skip
    with pytest.raises(ClientError):
        await s3_root.delete_object(
            Bucket=settings.s3_evidence_bucket, Key=e.storage_key, VersionId=e.version_id
        )
    async with tenant_tx(app_sessions, t) as s:
        events = (await s.execute(text(
            "SELECT payload FROM custody_events WHERE stream_id = :t"
            " AND event_type = 'audit.legal_hold_objects_flagged'"), {"t": t})).scalars().all()  # fmt: skip
    assert sum(p["count"] for p in events) == len(x["pages"])
    assert all(len(p["merkle_root"]) == 64 for p in events)
    # a second pass: nothing new, nothing counted twice
    await _run(flagger, t, hold)
    again = await _row(app_sessions, t, hold)
    assert (again.objects_flagged, again.objects_in_scope) == (len(x["pages"]), len(x["pages"]))


async def test_objects_linked_after_placement_are_flagged_by_the_next_pass(
    app_sessions: Sessions, s3: S3Client, s3_root: S3Client, hold_s3: S3Client, settings: Settings
) -> None:
    x = await _setup(app_sessions, s3, settings)
    t = x["tenant"]
    hold = await _hold(app_sessions, t, matter=x["a_matter"])
    flagger = HoldFlagger(app_sessions, s3, hold_s3, settings)
    await _run(flagger, t, hold)
    assert (await _row(app_sessions, t, hold)).objects_flagged == 1  # the one linked page
    # after the pass, A's (unsealed) job links every other page behind c's items
    async with tenant_tx(app_sessions, t) as s:
        rows = (await s.execute(text(
            "SELECT id, idempotency_key, content_hash, evidence_object_id FROM items WHERE job_id = :j"),
            {"j": x["c_job"]})).all()  # fmt: skip
        for page in {r.evidence_object_id for r in rows} - {x["shared"]}:
            group = [r for r in rows if r.evidence_object_id == page]
            sha = (await s.execute(text("SELECT sha256 FROM evidence_objects WHERE id = :e"),
                                   {"e": page})).scalar_one()  # fmt: skip
            ev = await append_batch(
                s, tenant_id=t, job_id=x["a_job"], unit_key="C1/2026-01-01", page_evidence_id=page,
                page_sha256=sha, items=[(r.idempotency_key, r.content_hash) for r in group],
                actor="tests",
            )  # fmt: skip
            for r in group:
                await s.execute(text(
                    "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id)"
                    " VALUES (:t, :j, :i, 'C1/2026-01-01', :e)"),
                    {"t": t, "j": x["a_job"], "i": r.id, "e": ev.id})  # fmt: skip
    await _run(flagger, t, hold)
    linked = {r.evidence_object_id for r in rows}  # A now links every page behind c's items
    row = await _row(app_sessions, t, hold)
    assert row.objects_flagged == row.objects_in_scope == len(linked) > 1
    flags = await _flag_state(s3_root, settings, app_sessions, t, list(linked))
    assert set(flags.values()) == {"ON"}


async def test_a_release_never_clears_a_flag_another_hold_still_needs(
    app_sessions: Sessions, s3: S3Client, s3_root: S3Client, hold_s3: S3Client, settings: Settings
) -> None:
    x = await _setup(app_sessions, s3, settings)
    t = x["tenant"]
    flagger = HoldFlagger(app_sessions, s3, hold_s3, settings)
    a_hold = await _hold(app_sessions, t, matter=x["a_matter"])  # covers the shared page
    c_hold = await _hold(app_sessions, t, matter=x["c_matter"])  # covers every page
    for h in (a_hold, c_hold):
        await _run(flagger, t, h)
    await _release(app_sessions, t, c_hold)
    await _run(flagger, t, c_hold)
    state = await _flag_state(s3_root, settings, app_sessions, t, x["pages"])
    assert state[x["shared"]] == "ON"  # A still holds it
    assert {v for k, v in state.items() if k != x["shared"]} == {"OFF"}
    row = await _row(app_sessions, t, c_hold)
    assert (row.flag_state, row.objects_cleared, row.flag_incidents) == (
        "cleared",
        len(x["pages"]) - 1,
        0,
    )
    await _release(app_sessions, t, a_hold)
    await _run(flagger, t, a_hold)
    assert (await _flag_state(s3_root, settings, app_sessions, t, [x["shared"]]))[
        x["shared"]
    ] == "OFF"


async def test_a_flag_removed_behind_our_back_is_an_incident_never_fixed(
    app_sessions: Sessions, s3: S3Client, s3_root: S3Client, hold_s3: S3Client, settings: Settings
) -> None:
    """D2: release refuses an object whose storage flag disagrees with the database."""
    x = await _setup(app_sessions, s3, settings)
    t = x["tenant"]
    flagger = HoldFlagger(app_sessions, s3, hold_s3, settings)
    hold = await _hold(app_sessions, t, matter=x["c_matter"])
    await _run(flagger, t, hold)
    async with tenant_tx(app_sessions, t) as s:
        e = (await s.execute(text("SELECT storage_key, version_id FROM evidence_objects WHERE id = :i"),
                             {"i": x["pages"][0]})).one()  # fmt: skip
    await s3_root.put_object_legal_hold(  # tampering: root clears one flag
        Bucket=settings.s3_evidence_bucket,
        Key=e.storage_key,
        VersionId=e.version_id,
        LegalHold={"Status": "OFF"},
    )
    await _release(app_sessions, t, hold)
    await _run(flagger, t, hold)
    row = await _row(app_sessions, t, hold)
    assert (row.flag_incidents, row.objects_cleared) == (1, len(x["pages"]) - 1)
    async with tenant_tx(app_sessions, t) as s:
        incident = (await s.execute(text(
            "SELECT payload FROM custody_events WHERE stream_id = :t"
            " AND event_type = 'audit.legal_hold_flag_incident'"), {"t": t})).scalar_one()  # fmt: skip
        alert = (await s.execute(text(
            "SELECT count(*) FROM alerts WHERE kind = 'legal_hold_flag_incident'"))).scalar_one()  # fmt: skip
    assert incident["kind"] == "flag_disagrees_with_database"
    assert incident["evidence_id"] == str(x["pages"][0]) and alert == 1


class CrashError(Exception):
    pass


@pytest.mark.parametrize("point", ["flags_set", "flag_cleared"])
async def test_a_crash_between_the_store_and_the_record_resumes_exactly(
    app_sessions: Sessions, s3: S3Client, s3_root: S3Client, hold_s3: S3Client, settings: Settings,
    point: str,
) -> None:  # fmt: skip
    """M18 §7: flags set but not recorded (the next run re-sets them, harmless); a flag cleared but
    not recorded (the recorded intent tells the resumed run it was ours, not an incident)."""
    x = await _setup(app_sessions, s3, settings)
    t = x["tenant"]
    hold = await _hold(app_sessions, t, matter=x["c_matter"])
    armed = {"on": True}

    async def hook(p: str) -> None:
        if p == point and armed["on"]:
            armed["on"] = False
            raise CrashError(p)

    crashing = HoldFlagger(app_sessions, s3, hold_s3, settings, hook=hook)
    if point == "flag_cleared":
        await _run(HoldFlagger(app_sessions, s3, hold_s3, settings), t, hold)
        await _release(app_sessions, t, hold)
    with pytest.raises(CrashError):
        await _run(crashing, t, hold)
    await _run(HoldFlagger(app_sessions, s3, hold_s3, settings), t, hold)  # the retry
    row = await _row(app_sessions, t, hold)
    state = set((await _flag_state(s3_root, settings, app_sessions, t, x["pages"])).values())
    if point == "flags_set":
        assert (row.flag_state, row.objects_flagged, row.objects_in_scope) == (
            "flagged",
            len(x["pages"]),
            len(x["pages"]),
        )
        assert state == {"ON"}
    else:
        assert (row.flag_state, row.objects_cleared, row.flag_incidents) == (
            "cleared",
            len(x["pages"]),
            0,
        )
        assert state == {"OFF"}


async def test_a_real_holds_worker_flags_then_clears_through_the_workflow(
    app_sessions: Sessions, s3: S3Client, s3_root: S3Client, settings: Settings, temporal: Any,
    tmp_path: Any,
) -> None:  # fmt: skip
    """``python -m edisc_worker --holds`` (the hold identity, nothing else) runs
    ``LegalHoldWorkflow``: a pass to ``flagged``, then after the release a pass to ``cleared``;
    SIGKILLed in the middle of a pass, a new worker finishes it with exact counts."""
    import asyncio
    import os
    import signal
    import sys
    from pathlib import Path

    from edisc_worker.contracts import hold_workflow_id

    x = await _setup(app_sessions, s3, settings)
    t = x["tenant"]
    hold = await _hold(app_sessions, t, matter=x["c_matter"])
    queue = f"holds-{uuid.uuid4().hex[:8]}"
    root = Path(__file__).resolve().parents[3]  # noqa: ASYNC240 (opens a log file once, like the render worker tests)

    async def spawn(n: int) -> Any:
        log = (Path(tmp_path) / f"holds-{n}.log").open("wb")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "edisc_worker", "--holds", "--holds-queue", queue,
            cwd=root, env={**os.environ}, stdout=log, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        log.close()
        return proc

    async def run_to(state: str) -> None:
        handle = await temporal.start_workflow(
            "LegalHoldWorkflow", args=[str(t), str(hold)], id=hold_workflow_id(hold) + queue,
            task_queue=queue,
        )  # fmt: skip
        async with asyncio.timeout(90):
            await handle.result()
        assert (await _row(app_sessions, t, hold)).flag_state == state

    worker = await spawn(1)
    try:
        await run_to("flagged")
        assert set(
            (await _flag_state(s3_root, settings, app_sessions, t, x["pages"])).values()
        ) == {"ON"}
        await _release(app_sessions, t, hold)
        worker.send_signal(signal.SIGKILL)  # between passes: the next worker takes over
        await worker.wait()
        worker = await spawn(2)
        await run_to("cleared")
        row = await _row(app_sessions, t, hold)
        assert (row.objects_flagged, row.objects_cleared, row.flag_incidents) == (
            len(x["pages"]),
            len(x["pages"]),
            0,
        )
        assert set(
            (await _flag_state(s3_root, settings, app_sessions, t, x["pages"])).values()
        ) == {"OFF"}
    finally:
        if worker.returncode is None:
            worker.send_signal(signal.SIGKILL)
            await worker.wait()


async def test_a_missing_version_is_an_incident_counted_in_scope_never_flagged(
    app_sessions: Sessions, s3: S3Client, hold_s3: S3Client, settings: Settings, connect: Any
) -> None:
    """Progress is what the store CONFIRMED (D2): a recorded version the store does not have is an
    incident (event + alert), counted in scope and pending, never as flagged."""
    x = await _setup(app_sessions, s3, settings)
    t = x["tenant"]
    su = await connect("superuser")
    try:  # the registry names a version the store never had
        await su.execute("SET session_replication_role = replica")
        await su.execute(
            "UPDATE edisc.evidence_objects SET version_id = $2 WHERE id = $1",
            x["pages"][0],
            str(uuid.uuid4()),  # a well-formed version id the store never issued
        )
    finally:
        await su.close()
    hold = await _hold(app_sessions, t, matter=x["c_matter"])
    await _run(HoldFlagger(app_sessions, s3, hold_s3, settings), t, hold)
    row = await _row(app_sessions, t, hold)
    assert (row.flag_state, row.objects_flagged, row.objects_in_scope, row.flag_incidents) == (
        "flagged",
        len(x["pages"]) - 1,
        len(x["pages"]),
        1,
    )
    async with tenant_tx(app_sessions, t) as s:
        kinds = (await s.execute(text(
            "SELECT payload->>'kind' FROM custody_events WHERE stream_id = :t"
            " AND event_type = 'audit.legal_hold_flag_incident'"), {"t": t})).scalars().all()  # fmt: skip
    assert kinds == ["version_missing"]
