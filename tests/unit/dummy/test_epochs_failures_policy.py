"""Epochs (incremental collections), deterministic failure injection, and the thread-parent policy."""

from __future__ import annotations

import pytest

from edisc_connector_dummy.connector import DummySourceError
from edisc_connector_dummy.dataset import Dataset, ts_to_datetime
from edisc_connectors_base.types import BatchKind, ThreadParentPolicy, WorkUnit

from .conftest import batches, connection, connector, full_scope, make_spec, messages, units


async def _state(spec, epoch):  # type: ignore[no-untyped-def]
    c, _ = connector()
    conn, scope = connection(spec, epoch), full_scope(spec, epoch)
    out = {}
    for u in await units(c, conn, scope):
        for b in await batches(c, conn, u, scope):
            if b.kind is BatchKind.HISTORY:
                for m in messages(b):
                    out[(u.conversation_id, m["ts"])] = m
    return out


# ------------------------------------------------------------------ epochs
async def test_second_collection_sees_new_messages_edits_deletions_and_reaction_changes() -> None:
    spec = make_spec()
    ds = Dataset(spec)
    e0, e1 = await _state(spec, 0), await _state(spec, 1)
    assert (
        len(e1) - len(e0) == spec.conversations * spec.messages_per_unit
    )  # one new day per conversation
    assert set(e0) <= set(e1)  # nothing disappears: deletions become tombstones

    conv0 = ds.conversations()[0].id
    forced = {slot: ds.ts(conv0, 0, slot) for slot in (5, 6, 7, 8)}
    edited0, edited1 = e0[(conv0, forced[5])], e1[(conv0, forced[5])]
    assert edited1["text"] != edited0["text"] and "edited" in edited1  # content edit
    hint0, hint1 = e0[(conv0, forced[6])], e1[(conv0, forced[6])]
    assert hint1["text"] == hint0["text"]  # edit marker moved, content did not
    assert hint1.get("edited") != hint0.get("edited") and "edited" in hint1
    deleted = e1[(conv0, forced[7])]
    assert (
        deleted["subtype"] == "message_deleted"
        and deleted["text"] == ""
        and "deleted_ts" in deleted
    )
    assert e0[(conv0, forced[7])].get("subtype") != "message_deleted"
    assert e1[(conv0, forced[8])].get("reactions") != e0[(conv0, forced[8])].get("reactions")

    kinds = {"edit": 0, "hint": 0, "delete": 0, "reaction": 0, "volatile_only": 0}
    for key, before in e0.items():
        after = e1[key]
        if after.get("subtype") == "message_deleted":
            kinds["delete"] += 1
        elif after["text"] != before["text"]:
            kinds["edit"] += 1
        elif after.get("edited") != before.get("edited"):
            kinds["hint"] += 1
        if (
            after.get("reactions") != before.get("reactions")
            and after.get("subtype") != "message_deleted"
        ):
            kinds["reaction"] += 1
        if (
            after != before
            and after["text"] == before["text"]
            and after.get("edited") == before.get("edited")
            and after.get("reactions") == before.get("reactions")
            and after.get("subtype") == before.get("subtype")
        ):
            kinds["volatile_only"] += 1
    assert all(v > 0 for v in kinds.values()), (
        kinds
    )  # incl. reply counters / file URL tokens changing alone


async def test_epoch_state_matches_the_oracle_model() -> None:
    spec = make_spec()
    ds = Dataset(spec)
    for epoch in (0, 1, 2):
        state = await _state(spec, epoch)
        for conv in ds.conversations():
            for d in range(ds.n_days(epoch)):
                for m in ds.unit_messages(conv.id, d, epoch):
                    raw = state[(conv.id, m.ts)]
                    assert (raw.get("subtype") == "message_deleted") == (m.deleted_ts is not None)
                    if m.deleted_ts is None:
                        assert raw["text"] == m.text
                        assert [f["id"] for f in raw.get("files", [])] == [f.id for f in m.files]


# ------------------------------------------------------------------ failures
FAILURES = {
    "seed": 3,
    "exception_rate": 0.15,
    "timeout_rate": 0.1,
    "throttle_rate": 0.1,
    "max_consecutive": 2,
    "retry_after_seconds": 0.01,
}


async def _run_with_failures():  # type: ignore[no-untyped-def]
    spec = make_spec(failures=FAILURES)
    c, limiter = connector()
    conn, scope = connection(spec), full_scope(spec)
    log: list[str] = []
    bodies: list[bytes] = []
    for u in await units(c, conn, scope):
        cursor = None
        while True:  # the caller (a Temporal activity) retries from the last good cursor
            try:
                async for b in c.fetch(conn, u, cursor, scope=scope):
                    bodies.append(b.body)
                    cursor = b.next_cursor
                    if cursor is None:
                        break
                else:
                    pass
                if cursor is None:
                    break
            except (DummySourceError, TimeoutError) as exc:
                log.append(f"{u.unit_key}:{type(exc).__name__}")
    return log, bodies, limiter


async def test_failure_injection_is_deterministic_and_recoverable() -> None:
    log1, bodies1, limiter1 = await _run_with_failures()
    log2, bodies2, limiter2 = await _run_with_failures()
    assert log1 == log2 and log1  # same failures, same order
    assert {entry.split(":")[1] for entry in log1} == {"DummySourceError", "TimeoutError"}
    assert limiter1.pauses and len(limiter1.pauses) == len(
        limiter2.pauses
    )  # 429s paused the bucket
    assert all(seconds == 0.01 for _, seconds in limiter1.pauses)
    clean_spec = make_spec()
    c, _ = connector()
    clean = [
        b.body
        for u in await units(c, connection(clean_spec), full_scope(clean_spec))
        for b in await batches(c, connection(clean_spec), u, full_scope(clean_spec))
    ]
    assert (
        bodies1 == bodies2 == clean
    )  # despite failures: the same data, nothing skipped or repeated


async def test_drops_with_true_counts_leave_a_detectable_gap() -> None:
    spec = make_spec(failures={"seed": 1, "drop_rate": 0.1})
    ds = Dataset(spec)
    c, _ = connector()
    conn, scope = connection(spec), full_scope(spec)
    missing = 0
    for u in await units(c, conn, scope):
        got = {
            m["ts"]
            for b in await batches(c, conn, u, scope)
            if b.kind is BatchKind.HISTORY
            for m in messages(b)
        }
        truth = ds.expected_ids(u.conversation_id, ds.day_index(u.day), 0)
        assert got < truth or got == truth
        assert await c.expected_count(conn, u) == len(
            truth
        )  # the source still reports the TRUE count
        missing += len(truth - got)
    assert missing > 0


async def test_source_that_cannot_count() -> None:
    spec = make_spec(count_mode="unavailable")
    c, _ = connector()
    conn = connection(spec)
    info = await c.validate_connection(conn)
    assert info.can_report_counts is False
    assert any("cannot report message counts" in b for b in info.blind_spots)
    for u in await units(c, conn, full_scope(spec)):
        assert await c.expected_count(conn, u) is None


async def test_every_request_takes_a_rate_limit_token_in_the_right_bucket() -> None:
    spec = make_spec(conversations=1, days=1)
    c, limiter = connector()
    conn, scope = connection(spec), full_scope(spec)
    u = (await units(c, conn, scope))[0]
    fetched = await batches(c, conn, u, scope)
    await c.expected_count(conn, u)
    [b async for b in c.fetch_directory(conn, None)]
    methods = [k.method for k in limiter.acquired]
    assert methods.count("fetch") == len(fetched)
    assert "expected_count" in methods and "directory" in methods
    assert {(k.tenant_id, k.source, k.workspace) for k in limiter.acquired} == {
        (conn.tenant_id, "dummy", spec.workspace_id)
    }


# ------------------------------------------------------------------ thread-parent policy
@pytest.mark.parametrize("policy", list(ThreadParentPolicy))
async def test_replies_in_range_with_parent_out_of_range(policy: ThreadParentPolicy) -> None:
    spec = make_spec()
    ds = Dataset(spec)
    c, _ = connector()
    conn = connection(spec)
    scope = full_scope(spec, first_day=1, policy=policy)  # day 0 is OUTSIDE the range
    conv = ds.conversations()[0].id
    u = WorkUnit(conv, ds.day(1))
    assert u in await units(c, conn, scope)
    assert WorkUnit(conv, ds.day(0)) not in await units(c, conn, scope)

    all_batches = await batches(c, conn, u, scope)
    history = {m["ts"] for b in all_batches if b.kind is BatchKind.HISTORY for m in messages(b)}
    context = [m for b in all_batches if b.kind is BatchKind.THREAD_CONTEXT for m in messages(b)]
    parents = ds.out_of_range_parents(conv, 1, 0, scope.date_from)
    assert parents, "the dataset guarantees a reply whose parent is on the previous day"
    assert history == ds.expected_ids(
        conv, 1, 0
    )  # the in-range unit is the same under every policy

    out_of_range = {m["ts"] for m in context if ts_to_datetime(m["ts"]) < scope.date_from}
    if policy is ThreadParentPolicy.REPLIES_ONLY:
        assert context == []
    elif policy is ThreadParentPolicy.INCLUDE_PARENT_ONLY:
        assert {m["ts"] for m in context} == set(parents) == out_of_range
    else:
        expected = {m.ts for p in parents for m in ds.thread(conv, p, 0)}
        assert {m["ts"] for m in context} == expected
        assert (
            set(parents) < out_of_range or set(parents) == out_of_range
        )  # parent + earlier replies
        assert any(
            ts in history for ts in expected
        )  # the full thread repeats in-range replies: dedup downstream
    # oracle agreement
    oracle = {
        m.ts
        for _, msgs in ds.thread_context(conv, 1, 0, scope.date_from, scope.date_to, policy)
        for m in msgs
    }
    assert {m["ts"] for m in context} == oracle


async def test_default_policy_is_the_proposed_one() -> None:
    spec = make_spec()
    assert full_scope(spec).thread_parent_policy is ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD


async def test_enumerate_scopes() -> None:
    from edisc_connectors_base.types import CollectionScope
    from edisc_core.schemas import ScopeType

    spec = make_spec()
    ds = Dataset(spec)
    c, _ = connector()
    conn = connection(spec)
    base = full_scope(spec)
    conv = ds.conversations()[1]
    one = CollectionScope(ScopeType.CHANNEL, conv.id, base.date_from, base.date_to)
    assert {u.conversation_id for u in await units(c, conn, one)} == {conv.id}
    member = conv.members[0]
    custodian = CollectionScope(ScopeType.CUSTODIAN, member, base.date_from, base.date_to)
    expected = {cv.id for cv in ds.conversations() if member in cv.members}
    assert {u.conversation_id for u in await units(c, conn, custodian)} == expected
    assert len(await units(c, conn, full_scope(spec, first_day=1, days=1))) == spec.conversations


@pytest.mark.parametrize("policy", list(ThreadParentPolicy))
async def test_mirror_case_parent_in_range_with_replies_after_the_range(
    policy: ThreadParentPolicy,
) -> None:
    spec = make_spec()
    ds = Dataset(spec)
    c, _ = connector()
    conn = connection(spec)
    scope = full_scope(
        spec, first_day=0, days=1, policy=policy
    )  # only day 0; day 1 is AFTER the range
    conv = ds.conversations()[0].id
    u = WorkUnit(conv, ds.day(0))
    mirror = ds.after_range_threads(conv, 0, 0, scope.date_to)
    assert mirror, "day-0 parents with day-1 replies are guaranteed"
    context = [
        m
        for b in await batches(c, conn, u, scope)
        if b.kind is BatchKind.THREAD_CONTEXT
        for m in messages(b)
    ]
    after = {m["ts"] for m in context if ts_to_datetime(m["ts"]) >= scope.date_to}
    if policy is ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD:
        expected_after = {
            m.ts for p in mirror for m in ds.thread(conv, p, 0) if m.sent_at >= scope.date_to
        }
        assert (
            after == expected_after and after
        )  # replies after the range are collected (marked out-of-range)
    else:
        assert after == set()


async def test_history_dialect_omits_deleted_messages_entirely() -> None:
    spec = make_spec(dialect="slack_history")
    ds = Dataset(spec)
    conv0 = ds.conversations()[0].id
    deleted_ts = ds.ts(conv0, 0, 7)  # forced deletion at epoch 1
    e0, e1 = await _state(spec, 0), await _state(spec, 1)
    assert (conv0, deleted_ts) in e0
    assert (conv0, deleted_ts) not in e1  # gone without a trace: no tombstone
    assert not any(m.get("subtype") == "message_deleted" for m in e1.values())
    c, _ = connector()
    u = WorkUnit(conv0, ds.day(0))
    truth = ds.unit_messages(conv0, 0, 1)
    assert await c.expected_count(connection(spec, 1), u) == len(
        [m for m in truth if m.deleted_ts is None]
    )
    tombstone_spec = make_spec()
    assert (await _state(tombstone_spec, 1))[(conv0, deleted_ts)]["subtype"] == "message_deleted"
