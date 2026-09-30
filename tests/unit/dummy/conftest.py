"""Helpers for dummy-connector tests. The in-memory limiter is a TEST double that records every
token request; production always uses the Redis RateLimiter."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.ratelimit import BucketKey, Grant, WaitCallback
from edisc_connectors_base.types import (
    CollectionScope,
    Connection,
    RawBatch,
    ThreadParentPolicy,
    WorkUnit,
)

TENANT = uuid.UUID("01900000-0000-7000-8000-000000000001")
CONN = uuid.UUID("01900000-0000-7000-8000-000000000002")


@dataclass
class RecordingLimiter:
    acquired: list[BucketKey] = field(default_factory=list)
    pauses: list[tuple[BucketKey, float]] = field(default_factory=list)

    async def acquire(self, key: BucketKey, *, on_wait: WaitCallback | None = None) -> Grant:
        self.acquired.append(key)
        return Grant(len(self.acquired), 0.0)

    async def pause(self, key: BucketKey, retry_after_seconds: float) -> int | None:
        self.pauses.append((key, retry_after_seconds))
        return 0


def make_spec(**overrides: Any) -> DatasetSpec:
    return DatasetSpec.model_validate(
        {"seed": 7, "conversations": 4, "days": 3, "messages_per_unit": 40, **overrides}
    )


def connection(spec: DatasetSpec, epoch: int = 0) -> Connection:
    return Connection(
        TENANT,
        CONN,
        "dummy",
        spec.workspace_id,
        {"spec": spec.model_dump(mode="json"), "epoch": epoch},
    )


def connector() -> tuple[DummyConnector, RecordingLimiter]:
    limiter = RecordingLimiter()
    return DummyConnector(limiter), limiter


def full_scope(
    spec: DatasetSpec,
    epoch: int = 0,
    *,
    first_day: int = 0,
    days: int | None = None,
    policy: ThreadParentPolicy = ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD,
) -> CollectionScope:
    ds = Dataset(spec)
    start = datetime.combine(ds.day(first_day), datetime.min.time(), tzinfo=UTC)
    return scope_for_days(
        "*",
        start,
        days if days is not None else ds.n_days(epoch) - first_day,
        thread_parent_policy=policy,
    )


async def batches(
    c: DummyConnector,
    conn: Connection,
    unit: WorkUnit,
    scope: CollectionScope,
    cursor: str | None = None,
) -> list[RawBatch]:
    return [b async for b in c.fetch(conn, unit, cursor, scope=scope)]


async def units(c: DummyConnector, conn: Connection, scope: CollectionScope) -> list[WorkUnit]:
    return [u async for u in c.enumerate(conn, scope)]


def messages(batch: RawBatch) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = json.loads(batch.body)["messages"]
    return msgs
