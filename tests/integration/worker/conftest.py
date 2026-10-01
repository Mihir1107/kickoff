"""Temporal workflow tests against the real Temporal server, Postgres, MinIO and Redis (nothing mocked).

Each test runs an in-process worker on its own task queue, so tests never pick up each other's tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Replayer, Worker
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.activities import Activities
from edisc_worker.contracts import JobInput, RunConfig
from edisc_worker.pipeline import CrashHooks, Pipeline
from edisc_worker.workflows import CollectionJobWorkflow, CollectUnitWorkflow

from ..normalizer.harness import Tenant, new_tenant

Sessions = async_sessionmaker[AsyncSession]
WORKFLOWS = [CollectionJobWorkflow, CollectUnitWorkflow]
GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "temporal"
RECORD = os.environ.get("EDISC_RECORD_HISTORIES") == "1"

# fast settings: small pages per activity and low continue-as-new thresholds so every test exercises
# continue-as-new of both workflows; short polls and retries
FAST = RunConfig(
    max_units_in_flight=3,
    pages_per_activity=1,
    unit_iterations_per_run=2,
    job_poll_seconds=2,
    start_to_close_seconds=120,
    heartbeat_timeout_seconds=10,
    control_timeout_seconds=60,
    retry_initial_seconds=0.1,
    retry_max_seconds=0.5,
    max_attempts=4,
    job_iterations_per_run=4,
)


def spec(**overrides: Any) -> DatasetSpec:
    return DatasetSpec.model_validate(
        {
            "seed": 23,
            "conversations": 3,
            "days": 2,
            "messages_per_unit": 14,
            "page_size": 6,
            "users": 8,
            **overrides,
        }
    )


@dataclass
class Harness:
    sessions: Sessions
    s3: S3Client
    settings: Settings
    temporal: Client
    limiter: RateLimiter

    def activities(
        self, *, settings: Settings | None = None, hooks: CrashHooks | None = None
    ) -> Activities:
        return Activities(
            self.sessions,
            self.s3,
            settings or self.settings,
            {"dummy": DummyConnector(self.limiter)},
            self.temporal,
            hooks or CrashHooks(),
        )

    @asynccontextmanager
    async def worker(
        self, queue: str, *, settings: Settings | None = None, hooks: CrashHooks | None = None
    ) -> AsyncIterator[Activities]:
        acts = self.activities(settings=settings, hooks=hooks)
        async with Worker(
            self.temporal, task_queue=queue, workflows=WORKFLOWS, activities=acts.all()
        ):
            yield acts

    async def tenant(self, sp: DatasetSpec, epoch: int = 0, **config: Any) -> Tenant:
        t = await new_tenant(self.sessions)
        await self.set_config(t, sp, epoch, **config)
        return t

    async def set_config(self, t: Tenant, sp: DatasetSpec, epoch: int = 0, **extra: Any) -> None:
        async with tenant_tx(self.sessions, t.tenant_id) as s:
            await s.execute(
                text(
                    "UPDATE connections SET external_org_id = :w, config = CAST(:c AS jsonb) WHERE id = :i"
                ),
                {
                    "w": sp.workspace_id,
                    "c": json.dumps({"spec": sp.model_dump(mode="json"), "epoch": epoch, **extra}),
                    "i": t.connection_id,
                },
            )

    async def create_job(self, t: Tenant, sp: DatasetSpec, epoch: int = 0) -> uuid.UUID:
        """What the API does (M13): the job row and its single scope, then the workflow start."""
        ds = Dataset(sp)
        job_id = new_id()
        await Pipeline(
            self.sessions, self.s3, self.settings, DummyConnector(self.limiter)
        ).start_job(
            tenant_id=t.tenant_id,
            job_id=job_id,
            matter_id=t.matter_id,
            connection_id=t.connection_id,
            scopes=[
                scope_for_days(
                    "*",
                    datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC),
                    ds.n_days(epoch),
                )
            ],
            requested_by="tester",
        )
        return job_id

    async def start(
        self, t: Tenant, job_id: uuid.UUID, queue: str, cfg: RunConfig = FAST
    ) -> WorkflowHandle[Any, str]:
        return await self.temporal.start_workflow(
            CollectionJobWorkflow.run,
            JobInput(str(t.tenant_id), str(job_id), cfg),
            id=str(job_id),
            task_queue=queue,
        )

    async def job(self, t: Tenant, job_id: uuid.UUID) -> Any:
        async with tenant_tx(self.sessions, t.tenant_id) as s:
            return (
                await s.execute(text("SELECT * FROM collection_jobs WHERE id = :j"), {"j": job_id})
            ).one()

    async def custody_types(self, t: Tenant, job_id: uuid.UUID) -> list[str]:
        async with tenant_tx(self.sessions, t.tenant_id) as s:
            return list(
                (
                    await s.execute(
                        text(
                            "SELECT event_type FROM custody_events WHERE stream_id = :j ORDER BY seq"
                        ),
                        {"j": job_id},
                    )
                ).scalars()
            )


@pytest.fixture
def harness(
    app_sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    temporal: Client,
    limiter: RateLimiter,
) -> Harness:
    return Harness(app_sessions, s3, settings, temporal, limiter)


def queue() -> str:
    return f"collect-dummy-test-{uuid.uuid4().hex[:12]}"


def fast(settings: Settings, **overrides: Any) -> Settings:
    return settings.model_copy(update={"activity_time_box_seconds": 30, **overrides})


async def all_histories(client: Client, job_id: uuid.UUID) -> list[Any]:
    """Every run of the parent and of each child (continue-as-new chains included)."""
    return [
        await client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history()
        async for wf in client.list_workflows(f"WorkflowId STARTS_WITH '{job_id}'")
    ]


async def replay_and_record(
    client: Client, job_id: uuid.UUID, name: str | None = None
) -> list[Any]:
    """Replay every recorded run against the current code (determinism), optionally save goldens."""
    histories: list[Any] = []
    for _ in range(50):  # visibility is eventually consistent
        histories = await all_histories(client, job_id)
        if any("/" not in h.workflow_id for h in histories):
            break
        await asyncio.sleep(0.2)
    assert histories, "no histories found in visibility"
    replayer = Replayer(workflows=WORKFLOWS)
    for h in histories:
        await replayer.replay_workflow(h)
    if RECORD and name is not None:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        picked = _pick(histories)
        for i, h in enumerate(picked):
            doc = {"workflow_id": h.workflow_id, "history": json.loads(h.to_json())}
            (GOLDEN / f"{name}-{i}.json").write_text(json.dumps(doc, indent=1, sort_keys=True))
    return histories


def _pick(histories: Sequence[Any]) -> list[Any]:
    """Keep goldens small: the parent runs plus one child chain."""
    parents = [h for h in histories if "/" not in h.workflow_id]
    children = sorted({h.workflow_id for h in histories if "/" in h.workflow_id})
    first_child = [h for h in histories if children and h.workflow_id == children[0]]
    return [*parents, *first_child]


def replace_cfg(**kw: Any) -> RunConfig:
    return replace(FAST, **kw)


def activity_attempts(histories: Sequence[Any]) -> list[int]:
    """Attempt number of every activity that started (an attempt > 1 means a retry: e.g. a missed
    heartbeat or an exceeded start-to-close)."""
    return [
        e.activity_task_started_event_attributes.attempt
        for h in histories
        for e in h.events
        if e.HasField("activity_task_started_event_attributes")
    ]
