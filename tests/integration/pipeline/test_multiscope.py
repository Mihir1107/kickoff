"""Several scopes per job (ADR 0005 amendment, M13.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector
from edisc_connector_dummy.dataset import Dataset, ts_to_datetime
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import (
    CollectionScope,
    Connection,
    ThreadParentPolicy,
)
from edisc_core.ids import new_id
from edisc_core.schemas import JobStatus, ScopeType
from edisc_core.settings import Settings
from edisc_custody.log import verify_chain
from edisc_db.session import tenant_tx
from edisc_worker.pipeline import Pipeline

from ...unit.dummy.conftest import RecordingLimiter
from ..normalizer.harness import Tenant, new_tenant, recorded
from ..normalizer.oracle import project
from .conftest import spec

Sessions = async_sessionmaker[AsyncSession]
POLICY = ThreadParentPolicy


def day_start(ds: Dataset, d: int) -> datetime:
    return datetime.combine(ds.day(d), datetime.min.time(), tzinfo=UTC)


def scope(
    ds: Dataset,
    kind: ScopeType,
    external_id: str,
    first_day: int,
    days: int,
    policy: ThreadParentPolicy = POLICY.INCLUDE_PARENT_AND_THREAD,
) -> CollectionScope:
    start = day_start(ds, first_day)
    return CollectionScope(kind, external_id, start, start + timedelta(days=days), policy)


async def run_scoped(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, sp: DatasetSpec,
    scopes: list[CollectionScope],
) -> uuid.UUID:  # fmt: skip
    conn = Connection(
        t.tenant_id, t.connection_id, "dummy", sp.workspace_id,
        {"spec": sp.model_dump(mode="json"), "epoch": 0},
    )  # fmt: skip
    p = Pipeline(sessions, s3, settings, DummyConnector(RecordingLimiter()))
    job = new_id()
    await p.start_job(
        tenant_id=t.tenant_id, job_id=job, matter_id=t.matter_id, connection_id=t.connection_id,
        scopes=scopes, requested_by="tester",
    )  # fmt: skip
    assert await p.run(tenant_id=t.tenant_id, job_id=job, conn=conn) is JobStatus.COMPLETED
    report = await verify_chain(sessions, s3, settings, tenant_id=t.tenant_id, stream_id=job)
    assert report.ok, report.errors
    return job


async def _units(sessions: Sessions, t: Tenant, job: uuid.UUID) -> dict[str, int]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT wu.unit_key, count(ws.scope_id) AS n FROM work_units wu"
                    " LEFT JOIN work_unit_scopes ws ON ws.job_id = wu.job_id AND ws.unit_key = wu.unit_key"
                    " WHERE wu.job_id = :j AND wu.kind = 'conversation_day' GROUP BY wu.unit_key"
                ),
                {"j": job},
            )
        ).all()
    return {r.unit_key: r.n for r in rows}


async def _links(sessions: Sessions, t: Tenant, job: uuid.UUID) -> list[Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT i.source_item_id, i.item_type, i.sent_at, ji.in_scope FROM job_items ji"
                        " JOIN items i ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id WHERE ji.job_id = :j"
                    ),
                    {"j": job},
                )
            ).all()
        )


async def test_overlapping_scopes_collect_each_unit_once_and_equal_the_single_scope_union(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(conversations=4, days=4)
    ds = Dataset(sp)
    convs = ds.conversations()
    custodian = convs[1].members[0]
    a = scope(ds, ScopeType.CHANNEL, convs[0].id, 0, 3)
    b = scope(ds, ScopeType.CUSTODIAN, custodian, 1, 3)
    t_both, t_a, t_b = (
        await new_tenant(app_sessions),
        await new_tenant(app_sessions),
        await new_tenant(app_sessions),
    )
    job = await run_scoped(app_sessions, s3, settings, t_both, sp, [a, b])
    job_a = await run_scoped(app_sessions, s3, settings, t_a, sp, [a])
    job_b = await run_scoped(app_sessions, s3, settings, t_b, sp, [b])

    units = await _units(app_sessions, t_both, job)
    units_a, units_b = (
        await _units(app_sessions, t_a, job_a),
        await _units(app_sessions, t_b, job_b),
    )
    assert set(units) == set(units_a) | set(units_b)  # the union, each unit once
    overlap = set(units_a) & set(units_b)
    assert overlap, "the scopes must overlap for this test to mean anything"
    assert all(units[k] == 2 for k in overlap) and all(units[k] == 1 for k in set(units) - overlap)

    # same policy in both scopes: the multi-scope job records exactly what the two single-scope jobs do
    got, want_a, want_b = [project(await recorded(app_sessions, t)) for t in (t_both, t_a, t_b)]
    for key in set(want_a) & set(want_b):
        assert want_a[key] == want_b[key]
    assert got == {**want_a, **want_b}


async def test_in_scope_is_the_union_of_the_ranges_applying_to_the_conversation(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(conversations=3, days=5, p_reply_same_day=0.05)
    ds = Dataset(sp)
    convs = ds.conversations()
    # two disjoint ranges on the same conversation (a gap on day 2), plus another conversation
    d1 = day_start(ds, 1)
    scopes = [
        scope(ds, ScopeType.CHANNEL, convs[0].id, 0, 2),
        scope(ds, ScopeType.CHANNEL, convs[0].id, 3, 2),
        scope(ds, ScopeType.CHANNEL, convs[1].id, 2, 1),
        # one day split by two partial-day ranges: the unit's own interval is the morning one, yet
        # evening messages are in scope through the other range
        CollectionScope(ScopeType.CHANNEL, convs[2].id, day_start(ds, 0), d1 + timedelta(hours=12)),
        CollectionScope(ScopeType.CHANNEL, convs[2].id, d1 + timedelta(hours=18), day_start(ds, 3)),
    ]
    t = await new_tenant(app_sessions)
    job = await run_scoped(app_sessions, s3, settings, t, sp, scopes)
    ranges: dict[str, list[tuple[datetime, datetime]]] = {}
    for sc in scopes:
        ranges.setdefault(sc.external_id, []).append((sc.date_from, sc.date_to))
    checked = 0
    for link in await _links(app_sessions, t, job):
        if link.item_type != "message" or link.sent_at is None:
            continue
        conv = link.source_item_id.split("/")[1]
        want = any(a <= link.sent_at < b for a, b in ranges[conv])
        assert link.in_scope is want, link.source_item_id
        checked += 1
    assert checked > 0
    evening = [
        x
        for x in await _links(app_sessions, t, job)
        if x.item_type == "message"
        and x.source_item_id.split("/")[1] == convs[2].id
        and x.sent_at is not None
        and d1 + timedelta(hours=18) <= x.sent_at < d1 + timedelta(days=1)
    ]
    assert evening and all(x.in_scope for x in evening)
    outside = [
        x for x in await _links(app_sessions, t, job) if x.item_type == "message" and not x.in_scope
    ]
    assert outside, "no thread context outside the ranges: the gap case was not exercised"


async def test_each_scope_gets_its_own_thread_policy(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(conversations=3, days=4, p_reply_same_day=0.05)
    ds = Dataset(sp)
    convs = ds.conversations()
    replies_only = scope(ds, ScopeType.CHANNEL, convs[0].id, 1, 2, POLICY.REPLIES_ONLY)
    full_threads = scope(ds, ScopeType.CHANNEL, convs[1].id, 1, 2, POLICY.INCLUDE_PARENT_AND_THREAD)
    t = await new_tenant(app_sessions)
    job = await run_scoped(app_sessions, s3, settings, t, sp, [replies_only, full_threads])
    context: dict[str, int] = {}
    for link in await _links(app_sessions, t, job):
        if link.item_type == "message" and not link.in_scope:
            conv = link.source_item_id.split("/")[1]
            context[conv] = context.get(conv, 0) + 1
    assert context.get(convs[0].id, 0) == 0  # replies_only: never any context
    assert context.get(convs[1].id, 0) > 0  # include_parent_and_thread: parents and threads


async def test_a_parent_inside_another_scopes_range_is_linked_once_and_in_scope(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(conversations=2, days=3, p_reply_same_day=0.05)
    ds = Dataset(sp)
    conv = ds.conversations()[0].id
    later = scope(ds, ScopeType.CHANNEL, conv, 2, 1)  # replies here...
    earlier = scope(ds, ScopeType.CHANNEL, conv, 1, 1)  # ...whose parents are here
    crossing = [
        m.thread_ts
        for m in ds.visible_messages(conv, 2, 0)
        if m.thread_ts
        and m.thread_ts != m.ts
        and ds.day_index(ts_to_datetime(m.thread_ts).date()) == 1
    ]
    assert crossing, "dataset has no day-1 parent with a day-2 reply"
    t = await new_tenant(app_sessions)
    job = await run_scoped(app_sessions, s3, settings, t, sp, [later, earlier])
    links = await _links(app_sessions, t, job)
    for thread_ts in crossing:
        sid = f"{sp.workspace_id}/{conv}/{thread_ts}"
        mine = [x for x in links if x.source_item_id == sid]
        assert len(mine) == 1 and mine[0].in_scope, sid
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        dupes = (
            await s.execute(
                text(
                    "SELECT count(*) - count(DISTINCT (source_item_id, version)) FROM items WHERE tenant_id = :t"
                ),
                {"t": t.tenant_id},
            )
        ).scalar_one()
    assert dupes == 0
