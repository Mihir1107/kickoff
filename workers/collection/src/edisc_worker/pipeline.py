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

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connectors_base.protocol import Connector
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
from edisc_core.schemas import JobStatus, ReconStatus, ScopeType
from edisc_core.settings import Settings
from edisc_core.time import day_bounds, utc_now
from edisc_custody.log import anchor_if_due, append, append_batch, seal_job_chain
from edisc_custody.recovery import recover_job_evidence
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_normalizer.model import (
    Derived,
    EvidenceRef,
    FileEvidence,
    FileUnavailable,
    NormalizeContext,
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
    normalize_directory_page,
    normalize_messages_page,
)
from edisc_normalizer.store import load_prior, persist

DIRECTORY_UNIT = "directory"
ACTOR = "collection-worker"


class MultiScopeNotSupportedError(ValueError):
    """A job must have exactly one date-range scope until per-unit scope resolution lands (M13).
    Rejected at creation: never silently use the first scope."""


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
    ) -> None:
        """Create the job (idempotent for a given ``job_id``) and record ``job_started``."""
        if len(scopes) != 1:
            raise MultiScopeNotSupportedError(
                f"a collection job needs exactly one date-range scope (got {len(scopes)}); "
                "multiple scopes per job are not supported yet: create one job per scope"
            )
        async with tenant_tx(self.sessions, tenant_id) as s:
            exists = (
                await s.execute(text("SELECT 1 FROM collection_jobs WHERE id = :j"), {"j": job_id})
            ).first()
            if exists:
                return
            await s.execute(
                text(
                    "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status, connector_version,"
                    " requested_by, started_at) VALUES (:j, :t, :m, :c, 'running', :v, :by, now())"
                ),
                {
                    "j": job_id,
                    "t": tenant_id,
                    "m": matter_id,
                    "c": connection_id,
                    "v": self.connector.version,
                    "by": requested_by,
                },
            )
            for scope in scopes:
                await s.execute(
                    text(
                        "INSERT INTO collection_scopes (id, tenant_id, job_id, scope_type, external_id, date_from,"
                        " date_to, thread_parent_policy) VALUES (:i, :t, :j, :st, :e, :f, :to, :p)"
                    ),
                    {
                        "i": new_id(),
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
                },
                anchor_every=self.anchor_every,
            )
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id
        )

    async def scope(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> CollectionScope:
        """The job's single scope (enforced at creation; re-checked here so bad data fails loudly)."""
        scopes = await self.scopes(tenant_id, job_id)
        if len(scopes) != 1:
            raise MultiScopeNotSupportedError(
                f"job {job_id} has {len(scopes)} scopes; expected exactly one"
            )
        return scopes[0]

    async def scopes(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> list[CollectionScope]:
        async with tenant_tx(self.sessions, tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT * FROM collection_scopes WHERE job_id = :j ORDER BY date_from, external_id"
                    ),
                    {"j": job_id},
                )
            ).all()
        return [
            CollectionScope(
                ScopeType(r.scope_type),
                r.external_id,
                r.date_from,
                r.date_to,
                ThreadParentPolicy(r.thread_parent_policy),
            )
            for r in rows
        ]

    async def enumerate_units(
        self, *, tenant_id: uuid.UUID, job_id: uuid.UUID, conn: Connection
    ) -> int:
        """Write every work unit (and the directory unit) to the DB. Idempotent."""
        units: dict[str, WorkUnit] = {}
        first_day: date | None = None
        for scope in await self.scopes(tenant_id, job_id):
            first_day = min(first_day or scope.date_from.date(), scope.date_from.date())
            async for unit in self.connector.enumerate(conn, scope):
                units.setdefault(unit.unit_key, unit)
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
        return len(units)

    async def pending_units(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> list[str]:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return list(
                (
                    await s.execute(
                        text(
                            "SELECT unit_key FROM work_units WHERE job_id = :j AND status <> 'done' ORDER BY unit_key"
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

    async def collect_pages(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        unit_key: str,
        conn: Connection,
        max_pages: int = 50,
    ) -> bool:
        """Process up to ``max_pages`` batches from the DB checkpoint. Returns True when the unit's pages
        are exhausted (ready to finalize)."""
        row = await self._unit(tenant_id, job_id, unit_key)
        if row.status == "done" or (row.status == "running" and row.recon_status == "access_lost"):
            return True
        if row.cursor is None and row.pages_done > 0:
            return True  # all pages applied; only finalize is left (a None cursor alone means "not started")
        scope = await self.scope(tenant_id, job_id)
        if row.kind == "directory":
            batches: AsyncIterator[RawBatch] = self.connector.fetch_directory(conn, row.cursor)
            unit = None
        else:
            unit = WorkUnit(row.conversation_id, row.day)
            if row.status == "pending":
                try:
                    expected = await self.connector.expected_count(conn, unit)
                except ConversationInaccessibleError as exc:
                    await self._record_access_lost(tenant_id, job_id, row, conn, exc, scope)
                    return True
                async with tenant_tx(self.sessions, tenant_id) as s:
                    await s.execute(
                        text(
                            "UPDATE work_units SET status = 'running', expected_count = :e, updated_at = now()"
                            " WHERE job_id = :j AND unit_key = :k AND status = 'pending'"
                        ),
                        {"e": expected, "j": job_id, "k": unit_key},
                    )
            batches = self.connector.fetch(conn, unit, row.cursor, scope=scope)
        cursor = row.cursor
        pages = 0
        try:
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
                    break  # someone else advanced this unit: re-read the checkpoint next time
                cursor = batch.next_cursor
                pages += 1
                if batch.next_cursor is None:
                    return True
                if pages >= max_pages:
                    return False
        except ConversationInaccessibleError as exc:
            await self._record_access_lost(tenant_id, job_id, row, conn, exc, scope)
            return True
        return cursor is None and pages > 0

    async def _files(
        self,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        retention: datetime,
        conn: Connection,
        body: bytes,
    ) -> dict[str, FileEvidence | FileUnavailable]:
        out: dict[str, FileEvidence | FileUnavailable] = {}
        for meta in file_refs(body):
            try:
                written = await self.writer.write_file(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    matter_retention_until=retention,
                    stream=self.connector.open_file(conn, meta.file_id),
                )
            except FileUnavailableError as exc:
                out[meta.file_id] = FileUnavailable(meta.file_id, exc.reason.value)
                continue
            out[meta.file_id] = FileEvidence(
                meta.file_id,
                written.sha256,
                written.size,
                EvidenceRef(written.evidence_id, written.storage_key),
            )
        return out

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
        scope: CollectionScope,
    ) -> bool:
        """One batch, exactly once. Returns False if the checkpoint had already moved (no-op)."""
        retention = await self._retention(tenant_id, job_id)
        # 1. network I/O first, outside any transaction: evidence written AND completed
        page = await self.writer.write_page(
            tenant_id=tenant_id,
            job_id=job_id,
            matter_retention_until=retention,
            stream=_one(batch.body),
        )
        page_ref = EvidenceRef(page.evidence_id, page.storage_key)
        directory = batch.kind is BatchKind.DIRECTORY
        files = (
            {} if directory else await self._files(tenant_id, job_id, retention, conn, batch.body)
        )
        await self.hooks.hit("after_evidence")

        ctx = NormalizeContext(
            tenant_id,
            self.connector.source,
            conn.workspace_id,
            None if directory else unit.conversation_id if unit else None,
            None if directory or unit is None else unit.day,
            None if directory else scope.date_from,
            None if directory else scope.date_to,
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
                        access_subject(conn.workspace_id, ctx.conversation_id or "")
                    }
                prior = await load_prior(
                    s, tenant_id=tenant_id, source=ctx.source, subjects=subjects
                )
                result = normalize_messages_page(
                    batch.body, ctx=ctx, page_ref=page_ref, prior=prior, files=files
                )
                extra = (
                    access_restored(ctx=ctx, page=batch.body, page_ref=page_ref, prior=prior)
                    if current.pages_done == 0 and batch.kind is BatchKind.HISTORY
                    else ()
                )
            items = (*result.items, *extra)
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
                    " last_page_evidence_id = CASE WHEN :hist THEN CAST(:ev AS uuid) ELSE last_page_evidence_id END,"
                    " updated_at = now() WHERE job_id = :j AND unit_key = :k"
                ),
                {
                    "c": batch.next_cursor,
                    "g": len(result.unavailable_files),
                    "hist": batch.kind is BatchKind.HISTORY,
                    "ev": page.evidence_id,
                    "j": job_id,
                    "k": unit_key,
                },
            )
        # 3. after commit
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id
        )
        return True

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
        new_links: list[tuple[str, str]] = []
        for key, d in sorted(by_key.items()):
            inserted = (
                await s.execute(
                    text(
                        "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id, in_scope)"
                        " VALUES (:t, :j, :i, :u, :e, :in) ON CONFLICT DO NOTHING RETURNING item_id"
                    ),
                    {
                        "t": ctx.tenant_id,
                        "j": job_id,
                        "i": stored.item_ids[key],
                        "u": unit_key,
                        "e": event_id,
                        "in": d.in_scope,
                    },
                )
            ).first()
            if inserted is not None:
                new_links.append((key, d.content_hash))
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
        scope: CollectionScope,
    ) -> None:
        """One conversation-level observation; the unit is closed as access_lost (never per-message)."""
        retention = await self._retention(tenant_id, job_id)
        page = await self.writer.write_page(
            tenant_id=tenant_id,
            job_id=job_id,
            matter_retention_until=retention,
            stream=_one(exc.response),
        )
        ctx = NormalizeContext(
            tenant_id,
            self.connector.source,
            conn.workspace_id,
            row.conversation_id,
            row.day,
            scope.date_from,
            scope.date_to,
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
            sid = access_subject(conn.workspace_id, row.conversation_id)
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
        last_page: tuple[bytes, EvidenceRef] | None = None
        if recon == ReconStatus.MATCHED.value and row.last_page_evidence_id is not None:
            body = b"".join(
                [
                    c
                    async for c in self.writer.open(
                        tenant_id=tenant_id, evidence_id=row.last_page_evidence_id
                    )
                ]
            )
            async with tenant_tx(self.sessions, tenant_id) as s:
                key: str = (
                    await s.execute(
                        text("SELECT storage_key FROM evidence_objects WHERE id = :e"),
                        {"e": row.last_page_evidence_id},
                    )
                ).scalar_one()
            last_page = (body, EvidenceRef(row.last_page_evidence_id, key))
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
                scope = await self.scope(tenant_id, job_id)
                ctx = NormalizeContext(
                    tenant_id,
                    self.connector.source,
                    conn.workspace_id,
                    row.conversation_id,
                    row.day,
                    scope.date_from,
                    scope.date_to,
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
                    last_page=last_page[0],
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
    async def _linked_messages(
        s: AsyncSession, job_id: uuid.UUID, unit_key: str, day: date
    ) -> set[str]:
        start, end = day_bounds(day)
        return set(
            (
                await s.execute(
                    text(
                        "SELECT DISTINCT i.source_item_id FROM job_items ji JOIN items i ON i.tenant_id = ji.tenant_id"
                        " AND i.id = ji.item_id WHERE ji.job_id = :j AND ji.unit_key = :u AND i.item_type = 'message'"
                        " AND i.sent_at >= :a AND i.sent_at < :b"
                    ),
                    {"j": job_id, "u": unit_key, "a": start, "b": end},
                )
            ).scalars()
        )

    async def _previously_observed_clean(
        self, s: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str, day: date
    ) -> set[str]:
        """Messages of this conversation-day seen by EARLIER CLEAN collections of the SAME unit (matched,
        no file gaps), excluding those already reported no-longer-observed. Never other units' context."""
        start, end = day_bounds(day)
        ids: set[str] = set(
            (
                await s.execute(
                    text(
                        "SELECT DISTINCT i.source_item_id FROM job_items ji"
                        " JOIN work_units wu ON wu.job_id = ji.job_id AND wu.unit_key = ji.unit_key"
                        " JOIN items i ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id"
                        " WHERE ji.unit_key = :u AND ji.job_id <> :j AND wu.kind = 'conversation_day' AND wu.status = 'done'"
                        " AND wu.recon_status = 'matched' AND wu.file_gaps = 0 AND i.item_type = 'message'"
                        " AND i.sent_at >= :a AND i.sent_at < :b"
                    ),
                    {"u": unit_key, "j": job_id, "a": start, "b": end},
                )
            ).scalars()
        )
        obs = await load_prior(
            s,
            tenant_id=tenant_id,
            source=self.connector.source,
            subjects=[f"{m}#observation" for m in ids],
        )
        return {m for m in ids if obs[f"{m}#observation"].observation_status != NO_LONGER_OBSERVED}

    # ------------------------------------------------------------------ job finalize
    async def finalize_job(self, *, tenant_id: uuid.UUID, job_id: uuid.UUID) -> JobStatus:
        await self.hooks.hit("during_finalize")
        async with tenant_tx(self.sessions, tenant_id) as s:
            job = (
                await s.execute(
                    text("SELECT status, finished_at FROM collection_jobs WHERE id = :j"),
                    {"j": job_id},
                )
            ).one()
        if job.finished_at is not None:
            return JobStatus(job.status)
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
                        "SELECT unit_key, kind, status, recon_status, file_gaps FROM work_units WHERE job_id = :j"
                    ),
                    {"j": job_id},
                )
            ).all()
            counted = [u for u in units if u.kind == "conversation_day"]
            if any(u.status != "done" or u.recon_status == "failed" for u in counted):
                status = JobStatus.FAILED
            elif any(u.recon_status in ("gap", "surplus", "access_lost") for u in counted):
                status = JobStatus.COMPLETED_WITH_GAPS
            elif any(u.recon_status == "unverifiable" for u in counted):
                status = JobStatus.COMPLETED_UNVERIFIED
            else:
                status = JobStatus.COMPLETED
            summary: dict[str, int] = {}
            for u in counted:
                summary[u.recon_status] = summary.get(u.recon_status, 0) + 1
            await s.execute(
                text(
                    "UPDATE collection_jobs SET status = :st, finished_at = now(), status_detail = CAST(:d AS jsonb)"
                    " WHERE id = :j"
                ),
                {"st": status.value, "d": json.dumps({"units": summary}), "j": job_id},
            )
            await append(
                s,
                tenant_id=tenant_id,
                stream_id=job_id,
                job_id=job_id,
                event_type="job_finished",
                actor=ACTOR,
                payload={"status": status.value, "units": summary},
                anchor_every=self.anchor_every,
            )
        await seal_job_chain(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, job_id=job_id
        )
        return status

    # ------------------------------------------------------------------ whole job (no Temporal)
    async def run(
        self, *, tenant_id: uuid.UUID, job_id: uuid.UUID, conn: Connection, max_pages: int = 3
    ) -> JobStatus:
        """Drive a job to completion from whatever state the DB holds (resumable at any point)."""
        await self.enumerate_units(tenant_id=tenant_id, job_id=job_id, conn=conn)
        for unit_key in await self.pending_units(tenant_id, job_id):
            while not await self.collect_pages(
                tenant_id=tenant_id,
                job_id=job_id,
                unit_key=unit_key,
                conn=conn,
                max_pages=max_pages,
            ):
                pass
            await self.finalize_unit(
                tenant_id=tenant_id, job_id=job_id, unit_key=unit_key, conn=conn
            )
        return await self.finalize_job(tenant_id=tenant_id, job_id=job_id)


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data
