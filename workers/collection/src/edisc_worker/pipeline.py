"""Collection pipeline: the activity bodies (Temporal wiring is M12). ADR 0005, 0006.

Per batch (one source page):
  1. Network I/O first, OUTSIDE any transaction: the page and every file it references are written to
     WORM and completed (or the file's refusal is recorded). The transaction only references completed
     evidence, and stays short (one page), far below ``lock_timeout``.
  2. ONE transaction: lock the work unit (``FOR NO KEY UPDATE``), confirm the checkpoint is still where
     this batch started (otherwise the batch was already applied: no-op), normalize, persist items,
     link them to the job (``in_scope`` on the LINK), append the custody batch event whose Merkle root
     covers exactly the NEW links, advance the checkpoint and counters. Commit.
  3. After commit: anchor the custody head if due.

Unit finalize: reconcile expected vs collected; only a CLEAN unit (matched, no file gaps) runs absence
detection, and only against earlier CLEAN collections of the same conversation-day.
Job finalize: recover interrupted evidence, aggregate reconciliation into the job status, seal.

Every function is idempotent and resumable from the database state alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connectors_base.protocol import Connector
from edisc_connectors_base.ratelimit import current_wait_callback
from edisc_connectors_base.types import (
    BatchKind,
    CollectionScope,
    Connection,
    ConversationInaccessibleError,
    FileUnavailableError,
    RawBatch,
    ThreadParentPolicy,
    WorkUnit,
)
from edisc_core.ids import new_id
from edisc_core.schemas import ItemType, JobStatus, ReconStatus, ScopeType
from edisc_core.settings import Settings
from edisc_core.time import day_bounds, ensure_utc, utc_now
from edisc_custody.log import anchor_if_due, append, append_batch, seal_job_chain
from edisc_custody.recovery import recover_job_evidence
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceIntegrityError, EvidenceWriter
from edisc_normalizer.model import (
    Derived,
    EvidenceRef,
    FileEvidence,
    FileUnavailable,
    NormalizeContext,
    PageResult,
)
from edisc_normalizer.slack import (
    NO_LONGER_OBSERVED,
    access_lost,
    access_restored,
    access_subject,
    directory_page_subjects,
    file_refs,
    finalize_unit,
    message_page_subjects,
    messages_fragment_hash,
    normalize_directory_page,
    normalize_messages_page,
)
from edisc_normalizer.store import load_prior, persist

DIRECTORY_UNIT = "directory"

# Items of a unit's conversation linked to this job, selected by id only (see
# Pipeline._messages_of_day for why there is no day filter). Conversation-wide, not unit-wide: with
# several scopes a message of this day may have been linked first as thread context by a neighbouring
# day's unit (job_items holds one link per item), and it still counts for this day.
_UNIT_LINKED_ITEMS = (
    "SELECT source_item_id, item_type, sent_at FROM items WHERE id = ANY(ARRAY("
    "SELECT ji.item_id FROM job_items ji WHERE ji.job_id = :j AND ji.unit_key IN ("
    "SELECT wu.unit_key FROM work_units wu WHERE wu.job_id = :j AND wu.conversation_id = ("
    "SELECT conversation_id FROM work_units WHERE job_id = :j AND unit_key = :u))))"
)
# ... and of the same unit in EARLIER CLEAN collections (matched, no file gaps)
_EARLIER_CLEAN_ITEMS = (
    "SELECT source_item_id, item_type, sent_at FROM items WHERE id = ANY(ARRAY("
    "SELECT ji.item_id FROM work_units wu JOIN job_items ji ON ji.job_id = wu.job_id"
    " AND ji.unit_key = wu.unit_key WHERE wu.unit_key = :u AND wu.job_id <> :j"
    " AND wu.kind = 'conversation_day' AND wu.status = 'done' AND wu.recon_status = 'matched'"
    " AND wu.file_gaps = 0))"
)
ACTOR = "collection-worker"


class NoScopeError(ValueError):
    """A collection job needs at least one scope."""


POLICY_ORDER = (
    ThreadParentPolicy.REPLIES_ONLY,
    ThreadParentPolicy.INCLUDE_PARENT_ONLY,
    ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD,
)


@dataclass(frozen=True)
class UnitScope:
    """How one unit is collected under a job's scopes (ADR 0005 amendment).

    ``scope``: what the connector is asked for: the merged range of the conversation's scopes that
    contains the unit's day, with the most inclusive policy among the scopes covering the unit.
    ``ranges``: every range applying to the conversation; an item is in scope if any contains it."""

    scope: CollectionScope
    ranges: tuple[tuple[datetime, datetime], ...]

    def context(
        self,
        tenant_id: uuid.UUID,
        source: str,
        workspace_id: str,
        conversation_id: str | None,
        day: date | None,
        dialect: str = "api",
    ) -> NormalizeContext:
        if conversation_id is None:  # directory pages are not date-scoped
            return NormalizeContext(
                tenant_id, source, workspace_id, None, None, None, None, dialect=dialect
            )
        return NormalizeContext(
            tenant_id,
            source,
            workspace_id,
            conversation_id,
            day,
            self.scope.date_from,
            self.scope.date_to,
            ranges=self.ranges,
            dialect=dialect,
        )


def merged(ranges: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


class CollectOutcome(StrEnum):
    DONE = "done"  # pages exhausted: finalize next
    MORE = "more"  # time box / page budget used: call again (resumes from the DB checkpoint)
    STOPPED = "stopped"  # the job is cancelled, failing, paused or closed


@dataclass(frozen=True)
class JobState:
    status: JobStatus
    stop_reason: str | None
    sealed: bool

    @property
    def accepts_batches(self) -> bool:
        return self.status is JobStatus.RUNNING and self.stop_reason is None and not self.sealed


@dataclass(frozen=True)
class UnitsOverview:
    ready: list[str]  # may be started now (pending, running without a live child, due retry_later)
    remaining: list[str]  # not yet done or failed
    next_retry_in: float | None  # seconds until the earliest retry_later unit is due


class _StopWaiting(Exception):  # noqa: N818 - control flow out of a limiter wait (never inside a transaction)
    def __init__(self, outcome: CollectOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


class CrashHooks:
    """Test seam: named points where the crash-matrix tests interrupt the pipeline. No-op in production."""

    async def hit(self, point: str) -> None:
        return None


@dataclass
class Pipeline:
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    settings: Settings
    connector: Connector
    hooks: CrashHooks = field(default_factory=CrashHooks)

    @property
    def writer(self) -> EvidenceWriter:
        return EvidenceWriter(self.sessions, self.s3, self.settings)

    @property
    def anchor_every(self) -> int:
        return self.settings.custody_anchor_every_n_batches

    # ------------------------------------------------------------------ job start / enumeration
    async def start_job(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        matter_id: uuid.UUID,
        connection_id: uuid.UUID,
        scopes: Sequence[CollectionScope],
        requested_by: str,
    ) -> list[uuid.UUID]:
        """Create the job (idempotent for a given ``job_id``) with its scopes (one or more, ADR 0005
        amendment) and record ``job_started``. Returns the scope ids in the given order."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            scope_ids = await self.create_job(
                s,
                tenant_id=tenant_id,
                job_id=job_id,
                matter_id=matter_id,
                connection_id=connection_id,
                scopes=scopes,
                requested_by=requested_by,
            )
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id
        )
        return scope_ids

    async def create_job(
        self,
        s: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        matter_id: uuid.UUID,
        connection_id: uuid.UUID,
        scopes: Sequence[CollectionScope],
        requested_by: str,
        workspace_id: uuid.UUID | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> list[uuid.UUID]:
        """``start_job`` inside the CALLER's tenant transaction (the API commits it together with its
        idempotency record); the caller anchors the job stream after commit. ``context`` (request id,
        idempotency key) is added to the ``job_started`` custody payload."""
        if not scopes:
            raise NoScopeError("a collection job needs at least one scope")
        exists = (
            await s.execute(text("SELECT 1 FROM collection_jobs WHERE id = :j"), {"j": job_id})
        ).first()
        if exists:
            rows: Iterable[uuid.UUID] = (
                await s.execute(
                    text(
                        "SELECT id FROM collection_scopes WHERE job_id = :j ORDER BY date_from, external_id, id"
                    ),
                    {"j": job_id},
                )
            ).scalars()
            return list(rows)
        scope_ids = [new_id() for _ in scopes]
        await s.execute(
            text(
                "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, workspace_id, status,"
                " connector_version, requested_by, started_at)"
                " VALUES (:j, :t, :m, :c, :w, 'running', :v, :by, now())"
            ),
            {
                "j": job_id,
                "t": tenant_id,
                "m": matter_id,
                "c": connection_id,
                "w": workspace_id,
                "v": self.connector.version,
                "by": requested_by,
            },
        )
        for scope_id, scope in zip(scope_ids, scopes, strict=True):
            await s.execute(
                text(
                    "INSERT INTO collection_scopes (id, tenant_id, job_id, scope_type, external_id, date_from,"
                    " date_to, thread_parent_policy) VALUES (:i, :t, :j, :st, :e, :f, :to, :p)"
                ),
                {
                    "i": scope_id,
                    "t": tenant_id,
                    "j": job_id,
                    "st": scope.scope_type.value,
                    "e": scope.external_id,
                    "f": scope.date_from,
                    "to": scope.date_to,
                    "p": scope.thread_parent_policy.value,
                },
            )
        await append(
            s,
            tenant_id=tenant_id,
            stream_id=job_id,
            job_id=job_id,
            event_type="job_started",
            actor=requested_by,
            payload={
                "connector": self.connector.source,
                "connector_version": self.connector.version,
                "connection_id": str(connection_id),
                "scopes": [
                    {
                        "type": sc.scope_type.value,
                        "id": sc.external_id,
                        "from": sc.date_from.isoformat(),
                        "to": sc.date_to.isoformat(),
                        "thread_parent_policy": sc.thread_parent_policy.value,
                    }
                    for sc in scopes
                ],
                **({"workspace_id": str(workspace_id)} if workspace_id else {}),
                **dict(context or {}),
            },
            anchor_every=self.anchor_every,
        )
        return scope_ids

    async def _scope_rows(
        self, tenant_id: uuid.UUID, job_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, CollectionScope]]:
        async with tenant_tx(self.sessions, tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT * FROM collection_scopes WHERE job_id = :j ORDER BY date_from, external_id, id"
                    ),
                    {"j": job_id},
                )
            ).all()
        return [
            (
                r.id,
                CollectionScope(
                    ScopeType(r.scope_type),
                    r.external_id,
                    r.date_from,
                    r.date_to,
                    ThreadParentPolicy(r.thread_parent_policy),
                ),
            )
            for r in rows
        ]

    async def scopes(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> list[CollectionScope]:
        return [scope for _, scope in await self._scope_rows(tenant_id, job_id)]

    async def unit_scope(self, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str) -> UnitScope:
        """The effective scope of one unit (ADR 0005 amendment). Units with no recorded coverage (none
        are expected) fall back to every scope of the job, never to one silently chosen scope."""
        rows = await self._scope_rows(tenant_id, job_id)
        by_id = dict(rows)
        async with tenant_tx(self.sessions, tenant_id) as s:
            unit = (
                await s.execute(
                    text(
                        "SELECT conversation_id, day, kind FROM work_units WHERE job_id = :j AND unit_key = :k"
                    ),
                    {"j": job_id, "k": unit_key},
                )
            ).one()
            covering: list[uuid.UUID] = list(
                (
                    await s.execute(
                        text(
                            "SELECT scope_id FROM work_unit_scopes WHERE job_id = :j AND unit_key = :k"
                        ),
                        {"j": job_id, "k": unit_key},
                    )
                ).scalars()
            )
            conversation: list[uuid.UUID] = list(
                (
                    await s.execute(
                        text(
                            "SELECT DISTINCT ws.scope_id FROM work_unit_scopes ws JOIN work_units wu"
                            " ON wu.job_id = ws.job_id AND wu.unit_key = ws.unit_key"
                            " WHERE ws.job_id = :j AND wu.conversation_id = :c"
                        ),
                        {"j": job_id, "c": unit.conversation_id},
                    )
                ).scalars()
            )
        cover = [by_id[i] for i in covering] or [scope for _, scope in rows]
        applying = [by_id[i] for i in conversation] or cover
        ranges = merged([(ensure_utc(sc.date_from), ensure_utc(sc.date_to)) for sc in applying])
        policy = max((sc.thread_parent_policy for sc in cover), key=POLICY_ORDER.index)
        day_start, day_end = day_bounds(unit.day)
        interval = next(
            ((a, b) for a, b in ranges if a < day_end and b > day_start),
            (ranges[0][0], ranges[-1][1]),
        )
        first = cover[0]
        return UnitScope(
            CollectionScope(first.scope_type, first.external_id, interval[0], interval[1], policy),
            tuple(ranges),
        )

    async def enumerate_units(
        self, *, tenant_id: uuid.UUID, job_id: uuid.UUID, conn: Connection
    ) -> int:
        """Write every work unit (and the directory unit) to the DB. Idempotent. Rerun jobs have an
        explicit unit list and are not enumerated."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            explicit: bool = (
                await s.execute(
                    text("SELECT explicit_units FROM collection_jobs WHERE id = :j"), {"j": job_id}
                )
            ).scalar_one()
        if explicit:
            return 0
        units: dict[str, WorkUnit] = {}
        coverage: dict[str, set[uuid.UUID]] = {}
        first_day: date | None = None
        for scope_id, scope in await self._scope_rows(tenant_id, job_id):
            first_day = min(first_day or scope.date_from.date(), scope.date_from.date())
            async for unit in self.connector.enumerate(conn, scope):
                units.setdefault(unit.unit_key, unit)
                coverage.setdefault(unit.unit_key, set()).add(scope_id)
        async with tenant_tx(self.sessions, tenant_id) as s:
            for unit in units.values():
                await s.execute(
                    text(
                        "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day)"
                        " VALUES (:t, :j, :k, :c, :d) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "t": tenant_id,
                        "j": job_id,
                        "k": unit.unit_key,
                        "c": unit.conversation_id,
                        "d": unit.day,
                    },
                )
            await s.execute(
                text(
                    "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day, kind)"
                    " VALUES (:t, :j, :k, '', :d, 'directory') ON CONFLICT DO NOTHING"
                ),
                {
                    "t": tenant_id,
                    "j": job_id,
                    "k": DIRECTORY_UNIT,
                    "d": first_day or utc_now().date(),
                },
            )
            for unit_key, scope_ids in coverage.items():
                for scope_id in scope_ids:
                    await s.execute(
                        text(
                            "INSERT INTO work_unit_scopes (tenant_id, job_id, unit_key, scope_id)"
                            " VALUES (:t, :j, :k, :s) ON CONFLICT DO NOTHING"
                        ),
                        {"t": tenant_id, "j": job_id, "k": unit_key, "s": scope_id},
                    )
        return len(units)

    async def pending_units(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> list[str]:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return list(
                (
                    await s.execute(
                        text(
                            "SELECT unit_key FROM work_units WHERE job_id = :j AND status NOT IN ('done', 'failed')"
                            " ORDER BY unit_key"
                        ),
                        {"j": job_id},
                    )
                ).scalars()
            )

    # ------------------------------------------------------------------ collecting one unit
    async def _unit(self, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str) -> Any:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return (
                await s.execute(
                    text("SELECT * FROM work_units WHERE job_id = :j AND unit_key = :k"),
                    {"j": job_id, "k": unit_key},
                )
            ).one()

    async def job_state(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> JobState:
        async with tenant_tx(self.sessions, tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT status, stop_reason, sealed_at FROM collection_jobs WHERE id = :j"
                    ),
                    {"j": job_id},
                )
            ).one()
        return JobState(JobStatus(row.status), row.stop_reason, row.sealed_at is not None)

    async def collect_pages(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        unit_key: str,
        conn: Connection,
        max_pages: int = 50,
        time_box_seconds: float | None = None,
        heartbeat: Callable[[dict[str, Any]], None] | None = None,
    ) -> CollectOutcome:
        """Process batches from the DB checkpoint until the unit is exhausted (DONE), the time box or
        ``max_pages`` is used up (MORE), or the job must stop (STOPPED: cancel, job failure, re-auth
        pause, closed). The job state is checked before EVERY batch and during every limiter wait;
        stopping never happens inside a transaction."""
        row = await self._unit(tenant_id, job_id, unit_key)
        if row.status in ("done", "failed") or (
            row.status == "running" and row.recon_status == "access_lost"
        ):
            return CollectOutcome.DONE
        if row.cursor is None and row.pages_done > 0:
            return CollectOutcome.DONE  # all pages applied; only finalize is left
        if not (await self.job_state(tenant_id, job_id)).accepts_batches:
            return CollectOutcome.STOPPED
        scope = await self.unit_scope(tenant_id, job_id, unit_key)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (time_box_seconds or self.settings.activity_time_box_seconds)
        state_checked = [loop.time(), True]
        cursor = row.cursor

        async def on_wait(reason: str, seconds: float) -> None:
            if heartbeat is not None:
                heartbeat(
                    {"unit_key": unit_key, "cursor": cursor, "waiting": reason, "seconds": seconds}
                )
            if loop.time() >= deadline:
                raise _StopWaiting(CollectOutcome.MORE)
            if loop.time() - float(state_checked[0]) >= 5:  # cached job-state check
                state_checked[0] = loop.time()
                if not (await self.job_state(tenant_id, job_id)).accepts_batches:
                    raise _StopWaiting(CollectOutcome.STOPPED)

        token = current_wait_callback.set(on_wait)
        try:
            if row.kind == "directory":
                batches: AsyncIterator[RawBatch] = self.connector.fetch_directory(conn, row.cursor)
                unit = None
            else:
                unit = WorkUnit(row.conversation_id, row.day)
                if row.status in ("pending", "retry_later", "paused"):
                    expected = row.expected_count
                    if row.status == "pending":
                        try:
                            expected = await self.connector.expected_count(conn, unit)
                        except ConversationInaccessibleError as exc:
                            await self._record_access_lost(tenant_id, job_id, row, conn, exc, scope)
                            return CollectOutcome.DONE
                    async with tenant_tx(self.sessions, tenant_id) as s:
                        await s.execute(
                            text(
                                "UPDATE work_units SET status = 'running', expected_count = :e, retry_after = NULL,"
                                " updated_at = now() WHERE job_id = :j AND unit_key = :k AND status <> 'done'"
                            ),
                            {"e": expected, "j": job_id, "k": unit_key},
                        )
                batches = self.connector.fetch(conn, unit, row.cursor, scope=scope.scope)
            pages = 0
            async for batch in batches:
                applied = await self.process_batch(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    unit_key=unit_key,
                    conn=conn,
                    batch=batch,
                    cursor_before=cursor,
                    unit=unit,
                    scope=scope,
                )
                await self.hooks.hit("after_commit")
                if not applied:
                    return (
                        CollectOutcome.MORE
                    )  # someone else advanced this unit: re-read the checkpoint
                cursor = batch.next_cursor
                pages += 1
                if heartbeat is not None:
                    heartbeat({"unit_key": unit_key, "cursor": cursor, "pages": pages})
                if batch.next_cursor is None:
                    return CollectOutcome.DONE
                if pages >= max_pages or loop.time() >= deadline:
                    return CollectOutcome.MORE
                if not (await self.job_state(tenant_id, job_id)).accepts_batches:
                    return CollectOutcome.STOPPED
        except ConversationInaccessibleError as exc:
            await self._record_access_lost(tenant_id, job_id, row, conn, exc, scope)
            return CollectOutcome.DONE
        except _StopWaiting as stop:
            return stop.outcome
        else:
            return CollectOutcome.DONE if cursor is None and pages > 0 else CollectOutcome.MORE
        finally:
            current_wait_callback.reset(token)

    async def _files(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        retention: datetime,
        conn: Connection,
        body: bytes,
        dialect: str = "api",
        select: frozenset[str] | None = None,
    ) -> dict[str, FileEvidence | FileUnavailable]:
        """Write every file the page references, ``evidence_file_concurrency`` at a time (memory stays
        bounded by that x the small-file threshold; every download still takes a rate-limit token).
        The first unexpected failure cancels the rest and is raised unchanged, so its error class
        decides the retry; failures of the cancelled writes are attached to it as notes."""
        file_ids = list(dict.fromkeys(meta.file_id for meta in file_refs(body, dialect, select)))
        if not file_ids:
            return {}
        gate = asyncio.Semaphore(self.settings.evidence_file_concurrency)
        writer = self.writer

        async def one(file_id: str) -> FileEvidence | FileUnavailable:
            async with gate:
                return await self._file(writer, tenant_id, job_id, retention, conn, file_id)

        tasks = [asyncio.create_task(one(file_id)) for file_id in file_ids]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException as exc:
            for task in tasks:
                task.cancel()
            for other in await asyncio.gather(*tasks, return_exceptions=True):
                if (
                    isinstance(other, BaseException)
                    and other is not exc
                    and not isinstance(other, asyncio.CancelledError)
                ):
                    exc.add_note(f"another file write of this page also failed: {other!r}")
            raise
        return dict(zip(file_ids, results, strict=True))

    async def _file(
        self,
        writer: EvidenceWriter,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        retention: datetime,
        conn: Connection,
        file_id: str,
    ) -> FileEvidence | FileUnavailable:
        attempt = 0
        while True:
            attempt += 1
            try:
                written = await writer.write_file(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    matter_retention_until=retention,
                    stream=self.connector.open_file(conn, file_id),
                )
            except FileUnavailableError as exc:
                # transient refusals (expired URL) get a bounded number of fresh attempts; permanent
                # ones are recorded at once. Either way the refusal is recorded, never skipped.
                if not exc.reason.transient or attempt >= self.settings.file_retry_attempts:
                    return FileUnavailable(file_id, exc.reason.value)
                await asyncio.sleep(self.settings.file_retry_backoff_seconds * 2 ** (attempt - 1))
                continue
            return FileEvidence(
                file_id,
                written.sha256,
                written.size,
                EvidenceRef(written.evidence_id, written.storage_key),
            )

    async def _retention(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> datetime:
        async with tenant_tx(self.sessions, tenant_id) as s:
            value: datetime = (
                await s.execute(
                    text(
                        "SELECT m.retention_until FROM collection_jobs j JOIN matters m ON m.id = j.matter_id WHERE j.id = :j"
                    ),
                    {"j": job_id},
                )
            ).scalar_one()
        return value

    async def process_batch(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        unit_key: str,
        conn: Connection,
        batch: RawBatch,
        cursor_before: str | None,
        unit: WorkUnit | None,
        scope: UnitScope,
    ) -> bool:
        """One batch, exactly once. Returns False if the checkpoint had already moved (no-op)."""
        retention = await self._retention(tenant_id, job_id)
        dialect = self.connector.dialect
        # 1. network I/O first, outside any transaction: evidence written AND completed (or, for an
        # entry of a locked export, referenced: the bytes are already evidence, ADR 0014)
        if batch.entry is not None:
            if hashlib.sha256(batch.body).hexdigest() != batch.entry.sha256:
                raise EvidenceIntegrityError(
                    f"{batch.entry.name}: bytes differ from the entry read"
                )
            page = await self.writer.register_archive_entry(
                tenant_id=tenant_id,
                job_id=job_id,
                archive_evidence_id=batch.entry.archive_evidence_id,
                entry_path=batch.entry.name,
                entry_raw_name=batch.entry.raw_name,
                entry_crc32=batch.entry.crc32,
                entry_compressed_size=batch.entry.compressed_size,
                sha256=batch.entry.sha256,
                size=batch.entry.size,
            )
        else:
            page = await self.writer.write_page(
                tenant_id=tenant_id,
                job_id=job_id,
                matter_retention_until=retention,
                stream=_one(batch.body),
            )
        page_ref = EvidenceRef(page.evidence_id, page.storage_key)
        directory = batch.kind is BatchKind.DIRECTORY
        files = (
            {}
            if directory
            else await self._files(
                tenant_id, job_id, retention, conn, batch.body, dialect, batch.select
            )
        )
        await self.hooks.hit("after_evidence")

        conversation = None if directory or unit is None else unit.conversation_id
        ctx = scope.context(
            tenant_id,
            self.connector.item_source,
            conn.workspace_id
            if conversation is None
            else await self.connector.item_workspace(conn, conversation),
            conversation,
            None if directory or unit is None else unit.day,
            self.connector.dialect,
        )
        # 2. one short transaction
        async with tenant_tx(self.sessions, tenant_id) as s:
            current = (
                await s.execute(
                    text(
                        "SELECT cursor, pages_done FROM work_units WHERE job_id = :j AND unit_key = :k FOR NO KEY UPDATE"
                    ),
                    {"j": job_id, "k": unit_key},
                )
            ).one()
            if current.cursor != cursor_before:
                return False  # this batch was already applied (retry after commit): no-op
            if directory:
                subjects = directory_page_subjects(batch.body, ctx=ctx)
                prior = await load_prior(
                    s, tenant_id=tenant_id, source=ctx.source, subjects=subjects
                )
                result = normalize_directory_page(
                    batch.body, ctx=ctx, page_ref=page_ref, prior=prior
                )
                extra: tuple[Derived, ...] = ()
            else:
                subjects = message_page_subjects(batch.body, ctx=ctx)
                if current.pages_done == 0 and batch.kind is BatchKind.HISTORY:
                    subjects = subjects | {
                        access_subject(ctx.workspace_id, ctx.conversation_id or "")
                    }
                prior = await load_prior(
                    s, tenant_id=tenant_id, source=ctx.source, subjects=subjects
                )
                result = normalize_messages_page(
                    batch.body,
                    ctx=ctx,
                    page_ref=page_ref,
                    prior=prior,
                    files=files,
                    select=batch.select,
                )
                extra = (
                    access_restored(ctx=ctx, page=batch.body, page_ref=page_ref, prior=prior)
                    if current.pages_done == 0 and batch.kind is BatchKind.HISTORY
                    else ()
                )
            items = (*result.items, *extra)
            archive_counts = (
                self._archive_counts(result, unit)
                if self.connector.archive_backed
                and batch.kind is BatchKind.HISTORY
                and unit is not None
                else None
            )
            await self._link_batch(
                s,
                ctx=ctx,
                job_id=job_id,
                unit_key=unit_key,
                items=items,
                page_ref=page_ref,
                page_sha256=page.sha256,
            )
            await s.execute(
                text(
                    "UPDATE work_units SET cursor = :c, pages_done = pages_done + 1, file_gaps = file_gaps + :g,"
                    " archive_accounted = coalesce(CAST(:acc AS integer), archive_accounted),"
                    " day_anomalies = day_anomalies + :anom,"
                    " last_page_evidence_id = CASE WHEN :hist THEN CAST(:ev AS uuid) ELSE last_page_evidence_id END,"
                    " last_page_fragment_hash = CASE WHEN :hist THEN CAST(:frag AS text) ELSE last_page_fragment_hash END,"
                    " updated_at = now() WHERE job_id = :j AND unit_key = :k"
                ),
                {
                    "c": batch.next_cursor,
                    "g": len(result.unavailable_files),
                    "hist": batch.kind is BatchKind.HISTORY,
                    "ev": page.evidence_id,
                    "frag": messages_fragment_hash(batch.body, dialect)
                    if batch.kind is BatchKind.HISTORY
                    else None,
                    "acc": None if archive_counts is None else archive_counts[0],
                    "anom": 0 if archive_counts is None else archive_counts[1],
                    "j": job_id,
                    "k": unit_key,
                },
            )
        # 3. after commit
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id
        )
        return True

    @staticmethod
    def _archive_counts(result: PageResult, unit: WorkUnit) -> tuple[int, int]:
        """(elements accounted for, elements whose own ts is not on the file's hinted day). Every
        element of a day file must become a message item (ADR 0014 section 4); the normalizer fails
        the batch loudly on any element it cannot interpret, so accounted = distinct messages derived."""
        start, end = day_bounds(unit.day)
        accounted = {d.source_item_id for d in result.items if d.item_type is ItemType.MESSAGE}
        anomalies = sum(
            1
            for d in result.items
            if d.item_type is ItemType.MESSAGE
            and d.sent_at is not None
            and not start <= d.sent_at < end
        )
        return len(accounted), anomalies

    async def _link_batch(
        self,
        s: AsyncSession,
        *,
        ctx: NormalizeContext,
        job_id: uuid.UUID,
        unit_key: str,
        items: Sequence[Derived],
        page_ref: EvidenceRef,
        page_sha256: str,
    ) -> None:
        """Persist items, link the NEW ones to the job under a pre-allocated custody event, append it."""
        stored = await persist(
            s, ctx=ctx, job_id=job_id, connector_version=self.connector.version, items=items
        )
        event_id = new_id()
        by_key: dict[str, Derived] = {}
        for d in items:
            by_key.setdefault(d.idempotency_key(ctx.tenant_id, ctx.source), d)
        ordered = sorted(by_key.items())
        linked: set[uuid.UUID] = set(
            (
                await s.execute(
                    text(
                        "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id, in_scope)"
                        " SELECT :t, :j, r.i, :u, :e, r.in_scope"
                        " FROM unnest(CAST(:i AS uuid[]), CAST(:ins AS boolean[])) AS r(i, in_scope)"
                        " ON CONFLICT DO NOTHING RETURNING item_id"
                    ),
                    {
                        "t": ctx.tenant_id,
                        "j": job_id,
                        "u": unit_key,
                        "e": event_id,
                        "i": [stored.item_ids[key] for key, _ in ordered],
                        "ins": [d.in_scope for _, d in ordered],
                    },
                )
            ).scalars()
        )
        new_links = [(key, d.content_hash) for key, d in ordered if stored.item_ids[key] in linked]
        await self.hooks.hit("mid_transaction")
        await append_batch(
            s,
            tenant_id=ctx.tenant_id,
            job_id=job_id,
            unit_key=unit_key,
            page_evidence_id=page_ref.evidence_id,
            page_sha256=page_sha256,
            items=new_links,
            actor=ACTOR,
            anchor_every=self.anchor_every,
            event_id=event_id,
        )

    async def _record_access_lost(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        row: Any,
        conn: Connection,
        exc: ConversationInaccessibleError,
        scope: UnitScope,
    ) -> None:
        """One conversation-level observation; the unit is closed as access_lost (never per-message)."""
        retention = await self._retention(tenant_id, job_id)
        page = await self.writer.write_page(
            tenant_id=tenant_id,
            job_id=job_id,
            matter_retention_until=retention,
            stream=_one(exc.response),
        )
        ctx = scope.context(
            tenant_id,
            self.connector.item_source,
            await self.connector.item_workspace(conn, row.conversation_id),
            row.conversation_id,
            row.day,
            self.connector.dialect,
        )
        async with tenant_tx(self.sessions, tenant_id) as s:
            locked = (
                await s.execute(
                    text(
                        "SELECT status, recon_status FROM work_units WHERE job_id = :j AND unit_key = :k FOR NO KEY UPDATE"
                    ),
                    {"j": job_id, "k": row.unit_key},
                )
            ).one()
            if locked.recon_status == "access_lost" or locked.status == "done":
                return
            sid = access_subject(ctx.workspace_id, row.conversation_id)
            prior = await load_prior(s, tenant_id=tenant_id, source=ctx.source, subjects=[sid])
            items = access_lost(
                ctx=ctx,
                reason=exc.reason.value,
                response=exc.response,
                response_ref=EvidenceRef(page.evidence_id, page.storage_key),
                prior=prior,
            )
            await self._link_batch(
                s,
                ctx=ctx,
                job_id=job_id,
                unit_key=row.unit_key,
                items=items,
                page_ref=EvidenceRef(page.evidence_id, page.storage_key),
                page_sha256=page.sha256,
            )
            await s.execute(
                text(
                    "UPDATE work_units SET status = 'running', recon_status = 'access_lost', access_lost_reason = :r,"
                    " updated_at = now() WHERE job_id = :j AND unit_key = :k"
                ),
                {"r": exc.reason.value, "j": job_id, "k": row.unit_key},
            )
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id
        )

    # ------------------------------------------------------------------ unit finalize
    async def finalize_unit(
        self, *, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str, conn: Connection
    ) -> str:
        """Reconcile; then (clean units only) absence detection. Idempotent. Returns recon status."""
        await self.hooks.hit("during_finalize")
        row = await self._unit(tenant_id, job_id, unit_key)
        if row.status == "done":
            return str(row.recon_status)
        if row.kind == "directory":
            recon, collected = ReconStatus.NOT_APPLICABLE.value, 0
        elif row.recon_status == "access_lost":
            recon, collected = "access_lost", 0
        elif self.connector.archive_backed:
            # every element of the day file accounted for, against the archive only (ADR 0014 s.4)
            collected = row.archive_accounted or 0
            matched = (
                row.expected_count is not None
                and row.archive_accounted == row.expected_count
                and row.file_gaps == 0
            )
            recon = ReconStatus.MATCHED_AGAINST_ARCHIVE.value if matched else ReconStatus.GAP.value
        else:
            collected = await self._collected(tenant_id, job_id, unit_key, row.day)
            if row.expected_count is None:
                recon = ReconStatus.UNVERIFIABLE.value
            elif collected < row.expected_count or row.file_gaps > 0:
                recon = ReconStatus.GAP.value
            elif collected > row.expected_count:
                recon = ReconStatus.SURPLUS.value
            else:
                recon = ReconStatus.MATCHED.value
        absent: tuple[Derived, ...] = ()
        last_page: tuple[str, EvidenceRef] | None = None  # (message-list fragment hash, page)
        # absence detection compares with earlier clean collections of the source; an export is a
        # snapshot of unknown completeness, so it never reports anything as no longer observed
        if recon == ReconStatus.MATCHED.value and row.last_page_evidence_id is not None:
            async with tenant_tx(self.sessions, tenant_id) as s:
                key: str = (
                    await s.execute(
                        text("SELECT storage_key FROM evidence_objects WHERE id = :e"),
                        {"e": row.last_page_evidence_id},
                    )
                ).scalar_one()
            fragment = row.last_page_fragment_hash
            if fragment is None:  # unit written before migration 0014: read the page back once
                body = b"".join(
                    [
                        c
                        async for c in self.writer.open(
                            tenant_id=tenant_id, evidence_id=row.last_page_evidence_id
                        )
                    ]
                )
                fragment = messages_fragment_hash(body)
            last_page = (fragment, EvidenceRef(row.last_page_evidence_id, key))
            workspace = await self.connector.item_workspace(conn, row.conversation_id)
        async with tenant_tx(self.sessions, tenant_id) as s:
            locked = (
                await s.execute(
                    text(
                        "SELECT status FROM work_units WHERE job_id = :j AND unit_key = :k FOR NO KEY UPDATE"
                    ),
                    {"j": job_id, "k": unit_key},
                )
            ).one()
            if locked.status == "done":
                return recon
            if last_page is not None:
                scope = await self.unit_scope(tenant_id, job_id, unit_key)
                ctx = scope.context(
                    tenant_id,
                    self.connector.item_source,
                    workspace,
                    row.conversation_id,
                    row.day,
                    self.connector.dialect,
                )
                before = await self._previously_observed_clean(
                    s, tenant_id, job_id, unit_key, row.day
                )
                observed = await self._linked_messages(s, job_id, unit_key, row.day)
                missing = before - observed
                prior = await load_prior(
                    s,
                    tenant_id=tenant_id,
                    source=ctx.source,
                    subjects=[*missing, *(f"{m}#observation" for m in missing)],
                )
                absent = finalize_unit(
                    ctx=ctx,
                    previously_observed=before,
                    observed=observed,
                    prior=prior,
                    last_page_fragment_hash=last_page[0],
                    last_page_ref=last_page[1],
                )
                if absent:
                    page_sha: str = (
                        await s.execute(
                            text("SELECT sha256 FROM evidence_objects WHERE id = :e"),
                            {"e": last_page[1].evidence_id},
                        )
                    ).scalar_one()
                    await self._link_batch(
                        s,
                        ctx=ctx,
                        job_id=job_id,
                        unit_key=unit_key,
                        items=absent,
                        page_ref=last_page[1],
                        page_sha256=page_sha,
                    )
            await s.execute(
                text(
                    "UPDATE work_units SET status = 'done', recon_status = :r, collected_count = :c, updated_at = now()"
                    " WHERE job_id = :j AND unit_key = :k"
                ),
                {"r": recon, "c": collected, "j": job_id, "k": unit_key},
            )
            await append(
                s,
                tenant_id=tenant_id,
                stream_id=job_id,
                job_id=job_id,
                event_type="unit_reconciled",
                actor=ACTOR,
                payload={
                    "unit_key": unit_key,
                    "expected": row.expected_count,
                    "collected": collected,
                    "recon_status": recon,
                    "file_gaps": row.file_gaps,
                    "no_longer_observed": len(absent),
                    **(
                        {"basis": "archive", "day_anomalies": row.day_anomalies}
                        if self.connector.archive_backed and row.kind != "directory"
                        else {}
                    ),
                },
                anchor_every=self.anchor_every,
            )
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id
        )
        return recon

    async def _collected(
        self, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str, day: date
    ) -> int:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return len(await self._linked_messages(s, job_id, unit_key, day))

    @staticmethod
    async def _messages_of_day(
        s: AsyncSession, statement: str, params: dict[str, Any], day: date
    ) -> set[str]:
        """Messages of ``day`` among the items ``statement`` selects by id (``_UNIT_LINKED_ITEMS`` or
        ``_EARLIER_CLEAN_ITEMS``).

        Items are fetched by id only and filtered here. Any day/type predicate in SQL let the planner
        (with statistics stale mid-load) drive the query from the day's items instead of the unit's
        links: ~300 ms per unit instead of ~3 ms."""
        start, end = day_bounds(day)
        rows = (await s.execute(text(statement), params)).all()
        return {
            r.source_item_id
            for r in rows
            if r.item_type == "message" and r.sent_at is not None and start <= r.sent_at < end
        }

    @classmethod
    async def _linked_messages(
        cls, s: AsyncSession, job_id: uuid.UUID, unit_key: str, day: date
    ) -> set[str]:
        return await cls._messages_of_day(
            s,
            _UNIT_LINKED_ITEMS,
            {"j": job_id, "u": unit_key},
            day,
        )

    async def _previously_observed_clean(
        self, s: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str, day: date
    ) -> set[str]:
        """Messages of this conversation-day seen by EARLIER CLEAN collections of the SAME unit (matched,
        no file gaps), excluding those already reported no-longer-observed. Never other units' context."""
        ids = await self._messages_of_day(
            s,
            _EARLIER_CLEAN_ITEMS,
            {"u": unit_key, "j": job_id},
            day,
        )
        obs = await load_prior(
            s,
            tenant_id=tenant_id,
            source=self.connector.item_source,
            subjects=[f"{m}#observation" for m in ids],
        )
        return {m for m in ids if obs[f"{m}#observation"].observation_status != NO_LONGER_OBSERVED}

    # ------------------------------------------------------------------ unit failure / retry later
    async def fail_unit(
        self, *, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str, error_type: str, error: str
    ) -> None:
        """Unit-scoped failure: loud (status, error, custody lifecycle event); other units go on."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            done = (
                await s.execute(
                    text(
                        "UPDATE work_units SET status = 'failed', recon_status = 'failed', last_error = :e, updated_at = now()"
                        " WHERE job_id = :j AND unit_key = :k AND status NOT IN ('done', 'failed') RETURNING unit_key"
                    ),
                    {"e": f"{error_type}: {error}"[:4000], "j": job_id, "k": unit_key},
                )
            ).first()
            if done is None:
                return
            await append(
                s,
                tenant_id=tenant_id,
                stream_id=job_id,
                job_id=job_id,
                event_type="unit_failed",
                actor=ACTOR,
                payload={"unit_key": unit_key, "error_type": error_type, "error": error[:2000]},
                anchor_every=self.anchor_every,
            )
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id
        )

    async def defer_unit(
        self, *, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str, error: str
    ) -> str:
        """Transient budget exhausted: ``retry_later`` with a cool-down, or ``failed`` once the unit has
        been failing for longer than the horizon. Returns the resulting status."""
        cooldown = self.settings.unit_retry_cooldown_seconds
        horizon = self.settings.unit_retry_horizon_seconds
        async with tenant_tx(self.sessions, tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE work_units SET failures = failures + 1, first_failure_at = coalesce(first_failure_at, now()),"
                        " last_error = :e, updated_at = now() WHERE job_id = :j AND unit_key = :k AND status NOT IN"
                        " ('done', 'failed') RETURNING first_failure_at, now() AS now"
                    ),
                    {"e": error[:4000], "j": job_id, "k": unit_key},
                )
            ).first()
            if row is None:
                return "unchanged"
            if (row.now - row.first_failure_at).total_seconds() < horizon:
                await s.execute(
                    text(
                        "UPDATE work_units SET status = 'retry_later', retry_after = now() + make_interval(secs => :c)"
                        " WHERE job_id = :j AND unit_key = :k"
                    ),
                    {"c": cooldown, "j": job_id, "k": unit_key},
                )
                return "retry_later"
        await self.fail_unit(
            tenant_id=tenant_id,
            job_id=job_id,
            unit_key=unit_key,
            error_type="TransientExhausted",
            error=f"failing for more than {horizon}s; last error: {error}",
        )
        return "failed"

    async def startable_units(
        self, tenant_id: uuid.UUID, job_id: uuid.UUID, limit: int
    ) -> UnitsOverview:
        async with tenant_tx(self.sessions, tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT unit_key, status, retry_after, retry_after <= now() AS due,"
                        " extract(epoch FROM retry_after - now()) AS wait FROM work_units WHERE job_id = :j ORDER BY unit_key"
                    ),
                    {"j": job_id},
                )
            ).all()
        ready = [
            r.unit_key
            for r in rows
            if r.status in ("pending", "running", "paused") or (r.status == "retry_later" and r.due)
        ]
        waits = [float(r.wait) for r in rows if r.status == "retry_later" and not r.due]
        remaining = [r.unit_key for r in rows if r.status not in ("done", "failed")]
        return UnitsOverview(ready[:limit], remaining, min(waits) if waits else None)

    async def unit_statuses(
        self, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_keys: Sequence[str]
    ) -> dict[str, str]:
        async with tenant_tx(self.sessions, tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT unit_key, status FROM work_units WHERE job_id = :j AND unit_key = ANY(:k)"
                    ),
                    {"j": job_id, "k": list(unit_keys)},
                )
            ).all()
        return {r.unit_key: r.status for r in rows}

    # ------------------------------------------------------------------ stop / pause / resume
    async def request_stop(
        self, *, tenant_id: uuid.UUID, job_id: uuid.UUID, reason: str, detail: str, actor: str
    ) -> bool:
        """Cancel or job-scoped failure: units stop at their next batch boundary. First request wins."""
        if reason not in ("cancel", "job_failure"):
            raise ValueError(f"unknown stop reason {reason}")
        async with tenant_tx(self.sessions, tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE collection_jobs SET stop_requested_at = now(), stop_reason = :r WHERE id = :j"
                        " AND stop_reason IS NULL AND finished_at IS NULL RETURNING id"
                    ),
                    {"r": reason, "j": job_id},
                )
            ).first()
            if row is None:
                return False
            await append(
                s,
                tenant_id=tenant_id,
                stream_id=job_id,
                job_id=job_id,
                event_type="cancel_requested" if reason == "cancel" else "job_failed",
                actor=actor,
                payload={"reason": reason, "detail": detail[:2000]},
                anchor_every=self.anchor_every,
            )
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id
        )
        return True

    async def pause_connection(
        self, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, reason: str
    ) -> list[uuid.UUID]:
        """Revoked/invalid credentials pause EVERY running job on the connection, with one alert."""
        paused: list[uuid.UUID] = []
        async with tenant_tx(self.sessions, tenant_id) as s:
            await s.execute(
                text(
                    "UPDATE connections SET status = 'reauth_required', updated_at = now() WHERE id = :c"
                ),
                {"c": connection_id},
            )
            jobs: Sequence[uuid.UUID] = (
                (
                    await s.execute(
                        text(
                            "UPDATE collection_jobs SET status = 'paused_awaiting_reauth' WHERE connection_id = :c"
                            " AND status = 'running' AND finished_at IS NULL RETURNING id"
                        ),
                        {"c": connection_id},
                    )
                )
                .scalars()
                .all()
            )
            for job in jobs:
                await s.execute(
                    text(
                        "INSERT INTO job_pauses (id, tenant_id, job_id, connection_id, reason) VALUES (:i, :t, :j, :c, :r)"
                    ),
                    {"i": new_id(), "t": tenant_id, "j": job, "c": connection_id, "r": reason},
                )
                await append(
                    s,
                    tenant_id=tenant_id,
                    stream_id=job,
                    job_id=job,
                    event_type="job_paused",
                    actor=ACTOR,
                    payload={"reason": reason, "connection_id": str(connection_id)},
                    anchor_every=self.anchor_every,
                )
                paused.append(job)
            if jobs:
                await s.execute(
                    text(
                        "INSERT INTO alerts (id, tenant_id, kind, connection_id, message) VALUES (:i, :t, 'reauth_required', :c, :m)"
                    ),
                    {
                        "i": new_id(),
                        "t": tenant_id,
                        "c": connection_id,
                        "m": f"Connection needs re-authorization ({reason}); {len(jobs)} job(s) paused.",
                    },
                )
        for job in paused:
            await anchor_if_due(
                self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job
            )
        return paused

    async def resume_connection(
        self, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: str
    ) -> list[uuid.UUID]:
        resumed: list[uuid.UUID] = []
        async with tenant_tx(self.sessions, tenant_id) as s:
            await s.execute(
                text("UPDATE connections SET status = 'active', updated_at = now() WHERE id = :c"),
                {"c": connection_id},
            )
            jobs: Sequence[uuid.UUID] = (
                (
                    await s.execute(
                        text(
                            "UPDATE collection_jobs SET status = 'running' WHERE connection_id = :c"
                            " AND status = 'paused_awaiting_reauth' RETURNING id"
                        ),
                        {"c": connection_id},
                    )
                )
                .scalars()
                .all()
            )
            for job in jobs:
                await s.execute(
                    text(
                        "UPDATE job_pauses SET resumed_at = now() WHERE job_id = :j AND resumed_at IS NULL"
                    ),
                    {"j": job},
                )
                await append(
                    s,
                    tenant_id=tenant_id,
                    stream_id=job,
                    job_id=job,
                    event_type="job_resumed",
                    actor=actor,
                    payload={"connection_id": str(connection_id)},
                    anchor_every=self.anchor_every,
                )
                resumed.append(job)
        for job in resumed:
            await anchor_if_due(
                self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job
            )
        return resumed

    # ------------------------------------------------------------------ job finalize
    async def finalize_job(self, *, tenant_id: uuid.UUID, job_id: uuid.UUID) -> JobStatus:
        """Recover evidence, then ONE transaction: ``job_finished`` event THEN the terminal status (the
        closed-job trigger rejects anything after), then seal and ``sealed_at``. Idempotent."""
        await self.hooks.hit("during_finalize")
        async with tenant_tx(self.sessions, tenant_id) as s:
            job = (
                await s.execute(
                    text(
                        "SELECT status, finished_at, stop_reason, sealed_at FROM collection_jobs WHERE id = :j"
                    ),
                    {"j": job_id},
                )
            ).one()
        if job.finished_at is None:
            await recover_job_evidence(
                self.writer,
                self.sessions,
                self.s3,
                self.settings,
                tenant_id=tenant_id,
                job_id=job_id,
                actor=ACTOR,
            )
            async with tenant_tx(self.sessions, tenant_id) as s:
                units = (
                    await s.execute(
                        text(
                            "SELECT unit_key, kind, status, recon_status FROM work_units WHERE job_id = :j"
                        ),
                        {"j": job_id},
                    )
                ).all()
                paused: float = (
                    await s.execute(
                        text(
                            "SELECT coalesce(sum(extract(epoch FROM coalesce(resumed_at, now()) - paused_at)), 0)"
                            " FROM job_pauses WHERE job_id = :j"
                        ),
                        {"j": job_id},
                    )
                ).scalar_one()
                counted = [u for u in units if u.kind == "conversation_day"]
                if job.stop_reason == "cancel":
                    status = JobStatus.CANCELLED
                elif job.stop_reason == "job_failure":
                    status = JobStatus.FAILED
                elif any(u.status not in ("done", "failed") for u in counted):
                    status = JobStatus.FAILED  # finalize without every unit settled: never clean
                elif any(u.status == "failed" for u in counted):
                    status = JobStatus.COMPLETED_WITH_FAILED_UNITS
                elif any(u.recon_status in ("gap", "surplus", "access_lost") for u in counted):
                    status = JobStatus.COMPLETED_WITH_GAPS
                elif any(u.recon_status == "unverifiable" for u in counted):
                    status = JobStatus.COMPLETED_UNVERIFIED
                elif self.connector.archive_backed:
                    status = JobStatus.COMPLETED_AGAINST_ARCHIVE  # never "completed" (ADR 0014)
                else:
                    status = JobStatus.COMPLETED
                summary: dict[str, int] = {}
                for u in counted:
                    summary[u.status if u.status == "failed" else u.recon_status] = (
                        summary.get(u.status if u.status == "failed" else u.recon_status, 0) + 1
                    )
                detail = {
                    "units": summary,
                    "paused_ms": round(
                        float(paused) * 1000
                    ),  # integer: custody payloads never carry floats
                    "stop_reason": job.stop_reason,
                }
                await append(
                    s,
                    tenant_id=tenant_id,
                    stream_id=job_id,
                    job_id=job_id,
                    event_type="job_cancelled" if status is JobStatus.CANCELLED else "job_finished",
                    actor=ACTOR,
                    payload={"status": status.value, **detail},
                    anchor_every=self.anchor_every,
                )
                await s.execute(
                    text(
                        "UPDATE collection_jobs SET status = :st, finished_at = now(), status_detail = CAST(:d AS jsonb)"
                        " WHERE id = :j"
                    ),
                    {"st": status.value, "d": json.dumps(detail), "j": job_id},
                )
        else:
            status = JobStatus(job.status)
        if job.sealed_at is None:
            await seal_job_chain(
                self.sessions, self.s3, self.settings, tenant_id=tenant_id, job_id=job_id
            )
            async with tenant_tx(self.sessions, tenant_id) as s:
                await s.execute(
                    text(
                        "UPDATE collection_jobs SET sealed_at = now() WHERE id = :j AND sealed_at IS NULL"
                    ),
                    {"j": job_id},
                )
        return status

    # ------------------------------------------------------------------ rerun failed units (a NEW job)
    async def create_rerun_job(
        self, *, tenant_id: uuid.UUID, original_job_id: uuid.UUID, requested_by: str
    ) -> uuid.UUID:
        """Failed units of a sealed job are re-run as a new job (the sealed original stays closed)."""
        job_id = new_id()
        original_scopes = await self._scope_rows(tenant_id, original_job_id)
        async with tenant_tx(self.sessions, tenant_id) as s:
            original = (
                await s.execute(
                    text("SELECT matter_id, connection_id FROM collection_jobs WHERE id = :j"),
                    {"j": original_job_id},
                )
            ).one()
            failed = (
                await s.execute(
                    text(
                        "SELECT unit_key, conversation_id, day, kind FROM work_units WHERE job_id = :j AND status = 'failed'"
                    ),
                    {"j": original_job_id},
                )
            ).all()
            coverage = (
                await s.execute(
                    text(
                        "SELECT ws.unit_key, ws.scope_id FROM work_unit_scopes ws JOIN work_units wu"
                        " ON wu.job_id = ws.job_id AND wu.unit_key = ws.unit_key"
                        " WHERE ws.job_id = :j AND wu.status = 'failed'"
                    ),
                    {"j": original_job_id},
                )
            ).all()
        new_ids = await self.start_job(
            tenant_id=tenant_id,
            job_id=job_id,
            matter_id=original.matter_id,
            connection_id=original.connection_id,
            scopes=[scope for _, scope in original_scopes],
            requested_by=requested_by,
        )
        renamed = dict(zip([sid for sid, _ in original_scopes], new_ids, strict=True))
        async with tenant_tx(self.sessions, tenant_id) as s:
            await s.execute(
                text(
                    "UPDATE collection_jobs SET rerun_of = :o, explicit_units = true WHERE id = :j"
                ),
                {"o": original_job_id, "j": job_id},
            )
            for u in failed:
                await s.execute(
                    text(
                        "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day, kind)"
                        " VALUES (:t, :j, :k, :c, :d, :kind)"
                    ),
                    {
                        "t": tenant_id,
                        "j": job_id,
                        "k": u.unit_key,
                        "c": u.conversation_id,
                        "d": u.day,
                        "kind": u.kind,
                    },
                )
            for c in coverage:  # the same scopes cover the re-run units
                await s.execute(
                    text(
                        "INSERT INTO work_unit_scopes (tenant_id, job_id, unit_key, scope_id)"
                        " VALUES (:t, :j, :k, :s)"
                    ),
                    {"t": tenant_id, "j": job_id, "k": c.unit_key, "s": renamed[c.scope_id]},
                )
        return job_id

    # ------------------------------------------------------------------ whole job (no Temporal)
    async def run(
        self, *, tenant_id: uuid.UUID, job_id: uuid.UUID, conn: Connection, max_pages: int = 3
    ) -> JobStatus:
        """Drive a job to completion from whatever state the DB holds, without Temporal (tests, tools)."""
        await self.enumerate_units(tenant_id=tenant_id, job_id=job_id, conn=conn)
        for unit_key in await self.pending_units(tenant_id, job_id):
            outcome = CollectOutcome.MORE
            while outcome is CollectOutcome.MORE:
                outcome = await self.collect_pages(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    unit_key=unit_key,
                    conn=conn,
                    max_pages=max_pages,
                )
            if outcome is CollectOutcome.DONE:
                await self.finalize_unit(
                    tenant_id=tenant_id, job_id=job_id, unit_key=unit_key, conn=conn
                )
        return await self.finalize_job(tenant_id=tenant_id, job_id=job_id)


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data
