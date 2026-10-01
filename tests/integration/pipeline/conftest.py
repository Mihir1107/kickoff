"""Pipeline test helpers: run whole jobs (with optional crashes) and check every invariant."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import Connection
from edisc_core.ids import new_id
from edisc_core.schemas import JobStatus
from edisc_core.settings import Settings
from edisc_custody.log import verify_chain
from edisc_db.session import tenant_tx
from edisc_worker.pipeline import CrashHooks, Pipeline

from ...unit.dummy.conftest import RecordingLimiter
from ..normalizer.harness import Tenant, recorded
from ..normalizer.oracle import expected, project

Sessions = async_sessionmaker[AsyncSession]


class SimulatedCrash(BaseException):
    """Like SIGKILL at a boundary: nothing after this point runs; open transactions roll back."""


class CrashAt(CrashHooks):
    def __init__(self, point: str, nth: int) -> None:
        self.point, self.nth, self.seen, self.fired = point, nth, 0, False

    async def hit(self, point: str) -> None:
        if point == self.point and not self.fired:
            self.seen += 1
            if self.seen == self.nth:
                self.fired = True
                raise SimulatedCrash(f"{point} #{self.nth}")


def spec(**overrides: Any) -> DatasetSpec:
    return DatasetSpec.model_validate(
        {
            "seed": 11,
            "conversations": 3,
            "days": 2,
            "messages_per_unit": 16,
            "page_size": 6,
            "users": 8,
            **overrides,
        }
    )


@dataclass
class JobRun:
    job_id: uuid.UUID
    status: JobStatus
    crashed: bool


async def run_job(
    sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    t: Tenant,
    sp: DatasetSpec,
    epoch: int,
    hooks: CrashHooks | None = None,
) -> JobRun:
    ds = Dataset(sp)
    conn = Connection(
        t.tenant_id,
        t.connection_id,
        "dummy",
        sp.workspace_id,
        {"spec": sp.model_dump(mode="json"), "epoch": epoch},
    )
    scope = scope_for_days(
        "*", datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC), ds.n_days(epoch)
    )
    job_id = new_id()

    def pipeline(h: CrashHooks | None) -> Pipeline:
        return Pipeline(
            sessions, s3, settings, DummyConnector(RecordingLimiter()), h or CrashHooks()
        )

    crashed = False
    try:
        p = pipeline(hooks)
        await p.start_job(
            tenant_id=t.tenant_id,
            job_id=job_id,
            matter_id=t.matter_id,
            connection_id=t.connection_id,
            scopes=[scope],
            requested_by="tester",
        )
        status = await p.run(tenant_id=t.tenant_id, job_id=job_id, conn=conn)
    except SimulatedCrash:
        crashed = True
        # resume from the database alone, in a fresh "process" (new connector, new pipeline)
        p = pipeline(None)
        await p.start_job(
            tenant_id=t.tenant_id,
            job_id=job_id,
            matter_id=t.matter_id,
            connection_id=t.connection_id,
            scopes=[scope],
            requested_by="tester",
        )
        status = await p.run(tenant_id=t.tenant_id, job_id=job_id, conn=conn)
    return JobRun(job_id, status, crashed)


async def units(sessions: Sessions, t: Tenant, job_id: uuid.UUID) -> list[Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text("SELECT * FROM work_units WHERE job_id = :j ORDER BY unit_key"),
                    {"j": job_id},
                )
            ).all()
        )


async def event_items(
    sessions: Sessions, t: Tenant, job_id: uuid.UUID | None = None
) -> dict[str, int]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT i.event_kind, count(*) FROM items i"
                    + (
                        " JOIN job_items ji ON ji.item_id = i.id AND ji.job_id = :j"
                        if job_id
                        else ""
                    )
                    + " WHERE i.tenant_id = :t AND i.item_type = 'event' GROUP BY i.event_kind"
                ),
                {"t": t.tenant_id, "j": job_id},
            )
        ).all()
    return {r[0]: r[1] for r in rows}


async def assert_invariants(
    sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    t: Tenant,
    sp: DatasetSpec,
    run: JobRun,
    epoch: int,
    *,
    oracle: bool = True,
) -> None:
    ds = Dataset(sp)
    # oracle-exact derived state
    if oracle:
        want, got = expected(ds, epoch), project(await recorded(sessions, t))
        problems = [k for k in set(want) | set(got) if want.get(k) != got.get(k)]
        assert not problems, (
            f"{len(problems)} differences, e.g. {sorted(problems)[:3]}: want {want.get(sorted(problems)[0])!r} got {got.get(sorted(problems)[0])!r}"
        )
    async with tenant_tx(sessions, t.tenant_id) as s:
        dupes = (
            await s.execute(
                text(
                    "SELECT count(*) - count(DISTINCT idempotency_key) FROM items WHERE tenant_id = :t"
                ),
                {"t": t.tenant_id},
            )
        ).scalar_one()
        pending = (
            await s.execute(
                text(
                    "SELECT count(*) FROM evidence_objects WHERE job_id = :j AND state = 'pending'"
                ),
                {"j": run.job_id},
            )
        ).scalar_one()
        dangling = (
            await s.execute(
                text(
                    "SELECT count(*) FROM job_items ji LEFT JOIN custody_events ce ON ce.id = ji.custody_event_id"
                    " WHERE ji.job_id = :j AND ce.id IS NULL"
                ),
                {"j": run.job_id},
            )
        ).scalar_one()
    assert dupes == 0
    assert pending == 0  # every interrupted evidence write was recovered at finalize
    assert dangling == 0
    report = await verify_chain(sessions, s3, settings, tenant_id=t.tenant_id, stream_id=run.job_id)
    assert report.ok, report.errors
    assert report.items_checked > 0
