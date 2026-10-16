"""A job's live stream (`GET /v1/jobs/{id}/stream`, M17 step 5, plan §4, ADR 0016 A2.4/A5).

Jobs here are rows plus REAL custody appends (`edisc_custody.log.append`), so the stream reads the
chain the pipeline writes; the end-to-end case with a real collection and its content is
`test_a_whole_collection_streams_no_evidence_content`. Expected values come from the chain and
the rows the test wrote, never from the stream under test. Settings are fast (poll 0.05 s,
re-check 0.3 s) except where a bound itself is the point (the 15 s revocation bound)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from edisc_api.auth import Caller
from edisc_api.stream import SessionWatch
from edisc_api.stream_events import EndReason, ResetReason
from edisc_core.ids import new_id
from edisc_custody.log import append
from edisc_db.session import tenant_tx

from ..custody.conftest import superuser
from .conftest import Api, TenantCtx, add_principal
from .oidc import Browser
from .sessions_flow import enable_login, host_of, sign_in
from .sse import Event, SseStream
from .test_jobs import make_world, worker  # noqa: F401 (the collection worker fixture)

FAST = {"api_stream_poll_seconds": 0.05, "api_stream_recheck_seconds": 0.3}


def tuned(api: Api, **settings: Any) -> Api:
    """The API with other stream settings (a fresh process's feeds and watch)."""
    new = api.settings.model_copy(update={**FAST, **settings})
    resources = dataclasses.replace(
        api.resources, settings=new, stream_feeds=None, session_watch=None,
        keyring=api.resources.keys, client_signer=api.resources.signer,
    )  # fmt: skip
    return Api(new, api.sessions, api.s3, api.temporal, resources, api.http)


@dataclasses.dataclass
class Job:
    id: uuid.UUID
    matter: uuid.UUID
    units: list[str]


async def new_job(api: Api, t: TenantCtx, units: int = 6) -> Job:
    w = await make_world(api, t)
    job = new_id()
    keys = [f"C{i:03d}/2026-01-05" for i in range(units)]
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status,"
                " connector_version, requested_by) VALUES (:i, :t, :m, :c, 'running', '0', 'tests')"
            ),
            {"i": job, "t": t.tenant_id, "m": uuid.UUID(w.matter), "c": uuid.UUID(w.connection)},
        )
        for key in keys:
            await s.execute(
                text(
                    "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day)"
                    " VALUES (:t, :j, :k, :c, :d)"
                ),
                {
                    "t": t.tenant_id,
                    "j": job,
                    "k": key,
                    "c": key.split("/")[0],
                    "d": date(2026, 1, 5),
                },
            )
        await append(s, tenant_id=t.tenant_id, stream_id=job, job_id=job, event_type="job_started",
                     actor="tests", payload={"units": len(keys)})  # fmt: skip
    return Job(job, uuid.UUID(w.matter), keys)


async def settle(api: Api, t: TenantCtx, job: Job, key: str, *, collected: int = 40) -> int:
    """The unit reconciled, as the pipeline records it: the row and its chain event, one tx."""
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(
            text(
                "UPDATE work_units SET status = 'done', recon_status = 'matched', expected_count = :n,"
                " collected_count = :n, updated_at = now() WHERE job_id = :j AND unit_key = :k"
            ),
            {"j": job.id, "k": key, "n": collected},
        )
        event = await append(s, tenant_id=t.tenant_id, stream_id=job.id, job_id=job.id,
                             event_type="unit_reconciled", actor="tests",
                             payload={"unit_key": key, "expected": collected})  # fmt: skip
    return int(event.seq)


async def seal(api: Api, t: TenantCtx, job: Job) -> None:
    conn = await superuser(api.settings)
    try:
        await conn.execute(
            "UPDATE collection_jobs SET status = 'completed', finished_at = now(),"
            " sealed_at = now() WHERE id = $1",
            job.id,
        )
    finally:
        await conn.close()


async def chain_units(api: Api, t: TenantCtx, job: Job) -> list[int]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return [
            int(r)
            for r in (
                await s.execute(
                    text(
                        "SELECT seq FROM custody_events WHERE stream_id = :j"
                        " AND event_type = 'unit_reconciled' ORDER BY seq"
                    ),
                    {"j": job.id},
                )
            ).scalars()
        ]


def stream(api: Api, t: TenantCtx, job: Job, token: str | None = None, **headers: str) -> SseStream:
    assert api.app is not None or api.client(t.subdomain)  # builds the app
    auth = {"authorization": f"Bearer {token or t.token(api.settings)}"}
    return SseStream(api.app, host_of(api, t), f"/v1/jobs/{job.id}/stream", {**auth, **headers})  # type: ignore[arg-type]


def unit_seqs(events: list[Event]) -> list[int]:
    return [int(e.id) for e in events if e.name == "units" and e.id is not None]


# ------------------------------------------------------------------ events, order, end
async def test_events_follow_the_chain_and_the_stream_ends_when_sealed(
    api: Api, tenant: TenantCtx
) -> None:
    api = tuned(api)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job) as sse:
        assert sse.status == 200
        assert sse.response_headers["content-type"].startswith("text/event-stream")
        assert sse.response_headers["cache-control"] == "no-cache, no-store, no-transform"
        assert sse.response_headers["x-accel-buffering"] == "no"
        first = await sse.next()
        assert first.name == "job" and first.data["status"] == "running"
        assert first.data["units_by_status"] == {"pending": 6}
        for key in job.units[:3]:
            await settle(api, tenant, job, key)
            await asyncio.sleep(0.12)  # several polls: one event per settle
        await seal(api, tenant, job)
        events = await sse.until("end")
    assert events[-1].data == {"reason": "sealed"}
    settled = [u["unit_key"] for e in events if e.name == "units" for u in e.data["units"]]
    assert settled == job.units[:3]
    seqs = unit_seqs(sse.all_events)
    assert seqs == sorted(set(seqs)) and seqs[-1] == (await chain_units(api, tenant, job))[-1]
    last_job = [e for e in events if e.name == "job"][-1]
    assert last_job.data["sealed"] and last_job.data["status"] == "completed"
    assert last_job.data["clean"] and last_job.data["clean_basis"] == "source"
    ids = [int((e.id or "0").split(".")[0]) for e in sse.all_events if e.id]
    assert ids == sorted(ids)  # monotonic: a snapshot's head bounds what came before it


async def test_a_seal_that_moves_neither_status_nor_chain_ends_the_stream(
    api: Api, tenant: TenantCtx
) -> None:
    """Found by an intermittent hang: the job finished (status and head moved, a snapshot went
    out), THEN its chain was sealed (only sealed_at moves). The feed must see the seal."""
    api = tuned(api)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job) as sse:
        await sse.next()
        conn = await superuser(api.settings)
        try:
            await conn.execute("UPDATE collection_jobs SET status = 'completed', finished_at = now()"
                               " WHERE id = $1", job.id)  # fmt: skip
            finished = await sse.next()
            assert finished.name == "job" and not finished.data["sealed"]
            await conn.execute("UPDATE collection_jobs SET sealed_at = now() WHERE id = $1", job.id)
        finally:
            await conn.close()
        events = await sse.until("end", within=5)
    assert events[-1].data == {"reason": "sealed"} and events[-2].data["sealed"]


# ------------------------------------------------------------------ reconnection
async def test_a_reconnect_with_last_event_id_misses_and_repeats_nothing(
    api: Api, tenant: TenantCtx
) -> None:
    api = tuned(api)
    job = await new_job(api, tenant, units=8)
    async with stream(api, tenant, job) as first:
        await first.next()  # the snapshot
        await settle(api, tenant, job, job.units[0])
        await settle(api, tenant, job, job.units[1])
        got = [await first.next()]
        while sum(len(e.data.get("units", [])) for e in got) < 2:
            got.append(await first.next())
    last_id = first.all_events[-1].id
    assert last_id is not None
    for key in job.units[2:5]:  # while the client is away
        await settle(api, tenant, job, key)
    async with stream(api, tenant, job, **{"Last-Event-ID": last_id}) as second:
        await settle(api, tenant, job, job.units[5])
        await asyncio.sleep(0.2)
        await seal(api, tenant, job)
        await second.until("end")
    # every settled unit exactly once, in chain order, across the two connections; the last
    # event id covers the chain's last unit event (an event may report several chain events)
    keys = [u["unit_key"] for e in first.all_events + second.all_events if e.name == "units"
            for u in e.data["units"]]  # fmt: skip
    assert keys == job.units[:6]
    seqs = unit_seqs(first.all_events) + unit_seqs(second.all_events)
    assert seqs == sorted(set(seqs)) and seqs[-1] == (await chain_units(api, tenant, job))[-1]
    snapshots = [e.id for e in first.all_events + second.all_events if e.name == "job"]
    assert len(snapshots) == len(set(snapshots))  # no snapshot twice


async def test_a_subscriber_ahead_of_the_feed_gets_no_unit_twice(
    api: Api, tenant: TenantCtx
) -> None:
    """The feed publishes chain events since ITS last head; a subscriber that joined since caught
    up further by itself. Deterministic: the feed polls every 2 s, so the settles and the join
    below happen between two of its polls, and its next event overlaps the catch-up."""
    api = tuned(api, api_stream_poll_seconds=2.0)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job) as keeper:  # keeps the feed alive and behind
        await keeper.next()
        await asyncio.sleep(0.2)  # past the feed's first poll: the next is ~2 s away
        await settle(api, tenant, job, job.units[0])
        await settle(api, tenant, job, job.units[1])
        async with stream(api, tenant, job) as late:
            caught_up = await late.until("job")  # its own catch-up: both units, then the snapshot
            await settle(api, tenant, job, job.units[2])
            live = await late.until("job", within=6)  # the feed's next poll: overlapping events
    keys = [u["unit_key"] for e in caught_up + live if e.name == "units" for u in e.data["units"]]
    assert keys == job.units[:3]  # each once
    assert keeper.all_events  # the feed was shared


async def test_a_reconnect_at_the_current_snapshot_resends_nothing(
    api: Api, tenant: TenantCtx
) -> None:
    api = tuned(api)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job) as first:
        snapshot = await first.next()
    async with stream(api, tenant, job, **{"Last-Event-ID": snapshot.id or ""}) as second:
        await asyncio.sleep(0.3)
        assert second.all_events == []  # nothing changed: nothing is repeated
        await settle(api, tenant, job, job.units[0])
        assert (await second.next()).name == "units"


async def test_an_unreadable_last_event_id_is_a_fresh_start(api: Api, tenant: TenantCtx) -> None:
    api = tuned(api)
    job = await new_job(api, tenant)
    await settle(api, tenant, job, job.units[0])
    async with stream(api, tenant, job, **{"Last-Event-ID": "not-an-id"}) as sse:
        assert [e.name for e in [await sse.next(), await sse.next()]] == ["units", "job"]


# ------------------------------------------------------------------ the replay's bound
async def _head(api: Api, t: TenantCtx, job: Job) -> int:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return int((await s.execute(text("SELECT max(seq) FROM custody_events WHERE stream_id = :j"),
                                    {"j": job.id})).scalar_one())  # fmt: skip


@pytest.mark.parametrize("away", [3, 4], ids=["exactly_at_the_cap", "one_over_the_cap"])
async def test_the_replay_is_bounded_and_one_over_the_cap_is_one_reset(
    api: Api, tenant: TenantCtx, away: int
) -> None:
    """EDISC_SSE_REPLAY_MAX = 3: three units settled while away are replayed; four give ONE
    ``reset`` (id = the head), the snapshot, then live events (reload from the lists)."""
    api = tuned(api, sse_replay_max=3)
    job = await new_job(api, tenant, units=6)
    async with stream(api, tenant, job) as first:
        snapshot = await first.next()
    for key in job.units[:away]:
        await settle(api, tenant, job, key)
    head = await _head(api, tenant, job)
    async with stream(api, tenant, job, **{"Last-Event-ID": snapshot.id or ""}) as second:
        caught = await second.until("job")
        await settle(api, tenant, job, job.units[away])
        live = await second.next()
    if away == 3:
        assert [e.name for e in caught] == ["units", "job"]
        assert [u["unit_key"] for u in caught[0].data["units"]] == job.units[:3]
    else:
        assert [(e.name, e.id, e.data) for e in caught[:1]] == [
            ("reset", str(head), {"reason": "replay_limit"})
        ]
        assert [e.name for e in caught[1:]] == ["job"] and caught[1].id.startswith(f"{head}.")  # type: ignore[union-attr]
    # live events continue after either: exactly the next unit
    assert live.name == "units" and [u["unit_key"] for u in live.data["units"]] == [job.units[away]]


async def test_a_fresh_start_over_the_cap_is_one_reset(api: Api, tenant: TenantCtx) -> None:
    api = tuned(api, sse_replay_max=2)
    job = await new_job(api, tenant)
    for key in job.units[:3]:
        await settle(api, tenant, job, key)
    async with stream(api, tenant, job) as sse:
        got = await sse.until("job")
    assert [(e.name, e.data.get("reason")) for e in got] == [
        ("reset", "replay_limit"),
        ("job", None),
    ]


async def _older_than(api: Api, t: TenantCtx, job: Job, seq: int, seconds: float) -> None:
    """Waits (a condition, by the database clock) until chain event ``seq`` is older than this."""
    while True:
        async with tenant_tx(api.sessions, t.tenant_id) as s:
            if (await s.execute(text(
                "SELECT now() - created_at > make_interval(secs => :a) FROM custody_events"
                " WHERE stream_id = :j AND seq = :q"), {"a": seconds, "j": job.id, "q": seq},
            )).scalar_one():  # fmt: skip
                return
        await asyncio.sleep(0.05)


async def test_a_replay_older_than_the_maximum_age_is_one_reset(
    api: Api, tenant: TenantCtx
) -> None:
    """The age of the OLDEST unit event the replay would send counts: from before the old unit,
    a reset; from just after it, the newer unit is replayed."""
    api = tuned(api, sse_replay_max_age_seconds=0.5)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job) as first:
        snapshot = await first.next()
    old = await settle(api, tenant, job, job.units[0])
    await _older_than(api, tenant, job, old, 0.5)
    await settle(api, tenant, job, job.units[1])
    async with stream(api, tenant, job, **{"Last-Event-ID": snapshot.id or ""}) as sse:
        got = await sse.until("job")
    assert [(e.name, e.data.get("reason")) for e in got] == [("reset", "replay_age"), ("job", None)]
    async with stream(api, tenant, job, **{"Last-Event-ID": str(old)}) as sse:
        got = await sse.until("job")
    assert [e.name for e in got] == ["units", "job"]
    assert [u["unit_key"] for u in got[0].data["units"]] == [job.units[1]]


async def test_a_position_past_the_head_is_one_reset(api: Api, tenant: TenantCtx) -> None:
    api = tuned(api)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job, **{"Last-Event-ID": "999999"}) as sse:
        got = await sse.until("job")
        await settle(api, tenant, job, job.units[0])
        live = await sse.next()
    assert [(e.name, e.data.get("reason")) for e in got] == [
        ("reset", "unknown_position"), ("job", None)
    ]  # fmt: skip
    assert [u["unit_key"] for u in live.data["units"]] == [job.units[0]]


# ------------------------------------------------------------------ continuous authorization
async def test_a_revoked_session_ends_the_stream_within_15_seconds(
    api: Api, tenant: TenantCtx
) -> None:
    """The production bound (decision D3): re-checked at most 15 s apart."""
    api = tuned(api, api_stream_recheck_seconds=15.0)
    await enable_login(api, tenant)
    browser = Browser()
    assert (await sign_in(api, tenant, browser)).status_code == 200
    job = await new_job(api, tenant)
    cookie = browser.cookie_header(host_of(api, tenant)) or ""
    sse = SseStream(api.app, host_of(api, tenant), f"/v1/jobs/{job.id}/stream",  # type: ignore[arg-type]
                    {"cookie": cookie, "sec-fetch-site": "same-origin"})  # fmt: skip
    async with sse:
        await sse.next()
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            await s.execute(text("UPDATE sessions SET revoked_at = now(), revoked_by = 'tests',"
                                 " revoke_reason = 'admin' WHERE revoked_at IS NULL"))  # fmt: skip
        revoked = time.monotonic()
        end = (await sse.until("end", within=20))[-1]
    assert end.data == {"reason": "session_revoked"}
    assert time.monotonic() - revoked <= 15.5
    assert await sse.ended()


async def test_the_session_watch_answers_no_older_than_half_the_bound(
    api: Api, tenant: TenantCtx
) -> None:
    """Every stream and download of a session shares one cached answer. A stream checks every
    ``interval`` and accepts an answer at most ``interval`` old: revocation to cut <= the bound.
    (Caching for the whole bound let a second stream of the session use an answer read just
    before the revocation, then wait a full period: up to twice the bound.)"""
    await enable_login(api, tenant)
    browser = Browser()
    assert (await sign_in(api, tenant, browser)).status_code == 200
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        sid, pid = (await s.execute(text("SELECT id, principal_id FROM sessions"
                                         " WHERE revoked_at IS NULL"))).one()  # fmt: skip
    caller = Caller(tenant.tenant_id, pid, "user", "iss", "sub", frozenset(), session_id=sid)
    watch = SessionWatch(api.sessions, seconds=1.0)
    assert watch.interval == 0.5
    assert await watch.check(caller) is None  # read now, cached
    read_at = time.monotonic()
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        await s.execute(text("UPDATE sessions SET revoked_at = now(), revoked_by = 'tests',"
                             " revoke_reason = 'admin' WHERE id = :s"), {"s": sid})  # fmt: skip
    # until the cached answer has aged one interval (before that it may still be used)
    await asyncio.sleep(max(0.0, watch.interval - (time.monotonic() - read_at)))
    assert await watch.check(caller) == EndReason.SESSION_REVOKED  # never an older one


async def test_a_stream_rechecks_the_session_every_half_the_bound(
    api: Api, tenant: TenantCtx
) -> None:
    """The other half of the bound (the watch's answer is at most ``interval`` old): a stream asks
    every ``interval`` = recheck / 2, so revocation to ``end`` <= recheck. The keepalive follows
    each re-check: the first comes at 2 s for a 4 s bound (at 4 s if it asked once per bound)."""
    api = tuned(api, api_stream_recheck_seconds=4.0)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job) as sse:
        await sse.next()
        opened = time.monotonic()
        first = await sse.keepalive(within=10)
    assert 1.9 <= first - opened < 3.0, first - opened


async def test_a_lost_permission_ends_the_stream(api: Api, tenant: TenantCtx) -> None:
    api = tuned(api)
    job = await new_job(api, tenant)
    pid, subject = await add_principal(api, tenant, roles=[("reviewer", "matter", job.matter)])
    async with stream(api, tenant, job, tenant.token(api.settings, subject=subject)) as sse:
        await sse.next()
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            await s.execute(text("UPDATE role_assignments SET revoked_at = now(), revoked_by = 'tests'"
                                 " WHERE principal_id = :p"), {"p": pid})  # fmt: skip
        end = (await sse.until("end", within=5))[-1]
    assert end.data == {"reason": "permission_lost"}


async def test_a_deactivated_principal_ends_its_bearer_stream(api: Api, tenant: TenantCtx) -> None:
    api = tuned(api)
    job = await new_job(api, tenant)
    pid, subject = await add_principal(api, tenant, roles=[("reviewer", "matter", job.matter)])
    async with stream(api, tenant, job, tenant.token(api.settings, subject=subject)) as sse:
        await sse.next()
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            await s.execute(text("UPDATE principals SET active = false WHERE id = :p"), {"p": pid})
        end = (await sse.until("end", within=5))[-1]
    assert end.data == {"reason": "principal_deactivated"}


@pytest.mark.parametrize("limit", ["idle", "absolute"])
async def test_an_open_stream_never_extends_the_session_and_its_limits_end_it(
    api: Api, tenant: TenantCtx, limit: str
) -> None:
    """D6: the stream authenticates passively; the idle and absolute limits still apply."""
    api = tuned(api)
    await enable_login(api, tenant)
    browser = Browser()
    assert (await sign_in(api, tenant, browser)).status_code == 200
    job = await new_job(api, tenant)
    conn = await superuser(api.settings)
    try:  # make the once-a-minute activity write due: a touching request would move it now
        await conn.execute("UPDATE sessions SET last_seen_at = now() - interval '10 minutes'"
                           " WHERE tenant_id = $1", tenant.tenant_id)  # fmt: skip
        before = await conn.fetchrow(
            "SELECT last_seen_at, idle_expires_at FROM sessions WHERE tenant_id = $1",
            tenant.tenant_id,
        )
        cookie = browser.cookie_header(host_of(api, tenant)) or ""
        sse = SseStream(api.app, host_of(api, tenant), f"/v1/jobs/{job.id}/stream",  # type: ignore[arg-type]
                        {"cookie": cookie, "sec-fetch-site": "same-origin"})  # fmt: skip
        async with sse:
            await sse.next()
            await settle(api, tenant, job, job.units[0])
            await sse.next()
            await asyncio.sleep(1.0)  # several re-checks
            after = await conn.fetchrow(
                "SELECT last_seen_at, idle_expires_at FROM sessions WHERE tenant_id = $1",
                tenant.tenant_id,
            )
            assert after == before  # the stream moved nothing
            # the idle expiry never passes the absolute one (a CHECK, even for a superuser): the
            # absolute case moves both (ADR 0016 step-2 note 9)
            columns = (
                ("idle_expires_at",)
                if limit == "idle"
                else ("idle_expires_at", "absolute_expires_at")
            )
            assignments = ", ".join(f"{c} = now() - interval '1 second'" for c in columns)
            await conn.execute(f"UPDATE sessions SET {assignments} WHERE tenant_id = $1",
                               tenant.tenant_id)  # fmt: skip
            end = (await sse.until("end", within=5))[-1]
    finally:
        await conn.close()
    assert end.data == {"reason": "session_expired"}


async def test_the_maximum_lifetime_ends_a_stream(api: Api, tenant: TenantCtx) -> None:
    api = tuned(api, api_stream_max_seconds=0.5)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job) as sse:
        end = (await sse.until("end", within=5))[-1]
    assert end.data == {"reason": "lifetime"}


async def test_streams_per_principal_are_capped(api: Api, tenant: TenantCtx) -> None:
    api = tuned(api, api_stream_per_principal=1)
    job = await new_job(api, tenant)
    async with stream(api, tenant, job) as sse:
        await sse.next()
        async with stream(api, tenant, job) as refused:  # answered at once, never a stream
            assert await refused.ended(within=5)
        assert refused.status == 429
        assert json.loads(bytes(refused.raw))["error"] == "too_many_streams"
    await asyncio.sleep(0.1)
    assert api.resources.streams.per_principal == {}  # released when the stream closed


async def test_an_invisible_job_is_404_and_no_stream_opens(api: Api, tenant: TenantCtx) -> None:
    api = tuned(api)
    job = await new_job(api, tenant)
    _, subject = await add_principal(api, tenant)  # no role anywhere
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        r = await c.get(f"/v1/jobs/{job.id}/stream")
    assert r.status_code == 404 and api.resources.streams.feeds == {}


# ------------------------------------------------------------------ backpressure
async def test_a_slow_client_is_bounded_then_ended_with_slow_consumer(
    api: Api, tenant: TenantCtx
) -> None:
    api = tuned(api, api_stream_queue_events=4)
    job = await new_job(api, tenant, units=30)
    async with stream(api, tenant, job) as sse:
        await sse.next()
        feed = api.resources.streams.feeds[job.id]
        (sub,) = feed.subscribers
        sse.pause()  # the client stops reading: the server's next write never completes
        await settle(api, tenant, job, job.units[0])
        await asyncio.sleep(0.15)
        for key in job.units[1:]:
            await settle(api, tenant, job, key)
            await asyncio.sleep(0.07)  # one poll each: two events (units, job) per settle
            assert sub.queue.qsize() <= 4  # bounded, however many events the job makes
        assert sub.overflowed and sub not in feed.subscribers  # dropped: no longer buffered
        sse.resume()
        events = await sse.until("end", within=5)
    assert events[-1].data == {"reason": "slow_consumer"}
    assert await sse.ended()


# ------------------------------------------------------------------ the module itself
def test_the_end_reasons_are_the_published_ones() -> None:
    from edisc_api import openapi_doc

    doc = openapi_doc.document()
    schemas = doc["components"]["schemas"]
    assert set(schemas["EndReason"]["enum"]) == {r.value for r in EndReason}
    assert set(schemas["ResetReason"]["enum"]) == {r.value for r in ResetReason}
    assert schemas["ResetEvent"]["properties"]["reason"] == {
        "$ref": "#/components/schemas/ResetReason"
    }
    op = doc["paths"]["/v1/jobs/{job_id}/stream"]["get"]
    assert op["x-sse-events"] == sorted(
        [
            "EndEvent",
            "EndReason",
            "JobEvent",
            "ResetEvent",
            "ResetReason",
            "UnitState",
            "UnitsEvent",
        ]
    )
    assert json.dumps(op["responses"]["200"]["content"]) == json.dumps(
        {"text/event-stream": {"schema": {"type": "string"}}}
    )


# ------------------------------------------------------------------ never evidence content
def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {s for v in value.values() for s in _strings(v)}
    if isinstance(value, list):
        return {s for v in value for s in _strings(v)}
    return set()


async def test_a_whole_collection_streams_no_evidence_content(
    api: Api,
    tenant: TenantCtx,
    worker: None,  # noqa: F811 (the collection worker fixture, imported from test_jobs)
) -> None:
    """A REAL dummy collection, streamed from start to seal: every unit is reported, and none of
    the job's content reaches the stream: no message text, file, conversation or user name (every
    string of its normalized items) and no custody payload value (hashes, roots, ids)."""
    from edisc_core.schemas import JobStatus, ReconStatus, UnitStatus

    api = tuned(api)
    w = await make_world(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    job = Job(uuid.UUID(job_id), uuid.UUID(w.matter), [])
    async with stream(api, tenant, job) as sse:
        events = await sse.until("end", within=90)
    assert events[-1].data == {"reason": "sealed"}
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        units = (await s.execute(text("SELECT unit_key, conversation_id, day FROM work_units"
                                      " WHERE job_id = :j"), {"j": job.id})).all()  # fmt: skip
        derived = (await s.execute(text(
            "SELECT d.derived FROM item_derivations d JOIN job_items ji ON ji.item_id = d.item_id"
            " WHERE ji.job_id = :j"), {"j": job.id})).scalars().all()  # fmt: skip
        payloads = (await s.execute(text("SELECT payload FROM custody_events WHERE stream_id = :j"),
                                    {"j": job.id})).scalars().all()  # fmt: skip
    reported = [u["unit_key"] for e in sse.all_events if e.name == "units" for u in e.data["units"]]
    assert sorted(reported) == sorted(u.unit_key for u in units)
    allowed = {u.unit_key for u in units} | {u.conversation_id for u in units}
    allowed |= {str(u.day) for u in units} | {
        v.value for v in (*JobStatus, *ReconStatus, *UnitStatus)
    }
    content = {x for d in derived for x in _strings(d)}
    custody = {x for p in payloads for x in _strings(p)}
    forbidden = {x for x in content | custody if len(x) >= 8 and x not in allowed}
    # positive controls: the sets really hold message text and custody hashes
    assert len({x for x in content if " " in x and x not in allowed}) > 20
    assert any(len(x) == 64 and all(c in "0123456789abcdef" for c in x) for x in forbidden)
    body = sse.raw.decode()
    leaked = sorted(x for x in forbidden if x in body)
    assert leaked == [], leaked[:5]


async def test_a_client_gone_during_the_catch_up_leaves_no_subscriber_or_connection(
    api: Api, tenant: TenantCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by a statement-counting test elsewhere: a disconnect while the subscriber's catch-up
    ran left it subscribed (its feed polled for ever) and could cancel a transaction mid-flight
    (a pooled connection unreturned). The catch-up is shielded; a late one unsubscribes itself."""
    from edisc_api import stream as stream_module

    api = tuned(api)
    job = await new_job(api, tenant)
    entered, release = asyncio.Event(), asyncio.Event()
    real = stream_module.read_changes

    async def held(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()  # inside the subscriber's transaction
        return await real(*args, **kwargs)

    monkeypatch.setattr(stream_module, "read_changes", held)
    pool = api.sessions.kw["bind"].sync_engine.pool
    sse = stream(api, tenant, job)
    await sse.__aenter__()
    await asyncio.wait_for(entered.wait(), 5)
    await sse.close()  # the client is gone while the catch-up holds a connection
    release.set()
    for _ in range(100):
        feeds = api.resources.streams.feeds
        if not feeds and pool.checkedout() == 0:
            break
        await asyncio.sleep(0.05)
    assert api.resources.streams.feeds == {}  # no subscriber left: the feed stopped
    assert pool.checkedout() == 0  # the transaction completed and returned its connection
