"""Renders of sealed jobs: the render's own custody stream and the activities of ``RenderWorkflow``
(ADR 0015 §7 and §14, M15 step 4).

A render moves ``requested -> rendering -> rendered -> completed``, or ends ``refused`` (before
anything is rendered) or ``failed``. Every step is one activity, idempotent from the database alone:
it acts only if the render is still in the status the step starts from, and it moves the status in the
SAME transaction as the custody event it appends. A retried or zombie attempt therefore finds the
status moved and writes nothing (the status, never a value that can repeat, is the fence).

- ``begin``: the source checks, then ``render_started`` or ``render_refused``. Refused: the job is not
  sealed, its matter or client is closed, its chain fails verification up to its head, or its seal
  anchor (listed from S3 versions, never from the DB) is not exactly one version agreeing with that
  head. ``render_started`` references the sealed job: id, status, completeness basis, final head
  (seq, hash) and seal anchor (key, VersionId), plus the renderer, Unicode and tzdata versions and the
  options (``include_context``, time zone, cap).
- ``render_files``: renders and stores (``render_and_store``: reconcile first, then write). Files are
  committed in batches of ``render_files_batch_size``: the rows plus one ``render_files_batch`` event
  whose Merkle root covers them (``edisc_custody.render_files``), with the natives (attachments kept
  outside the zip, ADR 0015 §20) first referenced by those files and their ``natives_root``. A batch is committed only when
  ``batches_done`` is exactly its index; a batch an earlier attempt committed must be re-rendered to
  exactly the recorded files, else it is an integrity incident. The render id fixes the storage keys
  (never the bytes), so a retry dedups against what is stored.
- ``complete``: ``render_completed`` (file count, batch count, the root over the batch roots, the
  native count and root over every native record, the reconciliation summary), then the seal: a forced anchor of the final head, recorded once on the
  render together with ``audit.render_completed`` (or ``.render_refused`` / ``.render_failed``).
- ``fail``: ``render_failed`` with the error, an alert, then the same seal.

Render events have ``render_id`` set and ``job_id`` NULL: a sealed job's chain is never appended to.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.chain import ANCHOR_FORMAT, anchor_key
from edisc_custody.log import anchor_if_due, append, verify_chain
from edisc_custody.render_files import (
    RENDER_BATCH_EVENT,
    RENDER_COMPLETED,
    RENDER_FAILED,
    RENDER_REFUSED,
    RENDER_STARTED,
    RenderFileError,
    batches_root,
    files_root,
    natives_root,
)
from edisc_db.session import tenant_tx
from edisc_evidence.worm import get_bytes, list_versions
from edisc_evidence.writer import EvidenceIntegrityError
from edisc_renderers.rsmf import RenderError, RenderOptions, runtime_versions
from edisc_worker.activities import _ticking
from edisc_worker.contracts import (
    ErrorClass,
    RenderFailure,
    RenderRef,
    render_task_queue,
    render_workflow_id,
)
from edisc_worker.errors import classify, describe, to_application_error
from edisc_worker.pipeline import CrashHooks
from edisc_worker.render_loader import RenderInputIntegrityError, RenderRefusedError
from edisc_worker.render_store import StoredFile, render_and_store

ACTOR = "system:render"
LIVE = ("requested", "rendering", "rendered")
FINAL = ("completed", "refused", "failed")


def native_record(row: Any) -> dict[str, Any]:
    """A ``render_natives`` row as its record (``NATIVE_FIELDS``)."""
    return {
        "ord": row.ord,
        "sha256": row.sha256,
        "size": row.size_bytes,
        "storage_key": row.storage_key,
        "version_id": row.version_id,
        "file_ords": list(row.file_ords),
    }


class RenderIntegrityError(RuntimeError):
    """What a render stored or recorded disagrees with what it renders now. Always an incident."""


class _Superseded(Exception):  # noqa: N818 - control flow: another attempt moved the render on
    pass


def options_hash(options: RenderOptions) -> str:
    return hashlib.sha256(canonical_json(options.as_payload())).hexdigest()


def options_of(row: Any) -> RenderOptions:
    o = row.options
    return RenderOptions(
        include_context=bool(o["include_context"]),
        time_zone=str(o["time_zone"]),
        cap=int(o["cap"]),
        external_over_bytes=int(o["external_over_bytes"]),
    )


@dataclass(frozen=True)
class CreatedRender:
    render_id: uuid.UUID
    created: bool  # False: a live render with the same identity already existed


async def create_render(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    matter_id: uuid.UUID,
    options: RenderOptions,
    requested_by: str,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    versions: Mapping[str, str] | None = None,
) -> CreatedRender:
    """Insert a render, or find the live one with the same identity (job, options hash, renderer,
    Unicode and tzdata versions). Concurrent identical calls: the unique index makes all but one wait
    and do nothing, then they read the winner. Failed and refused renders do not count."""
    versions = versions or runtime_versions()
    identity = {
        "t": tenant_id,
        "j": job_id,
        "h": options_hash(options),
        "rv": versions["renderer_version"],
        "uv": versions["unicode_version"],
        "tv": versions["tzdata_version"],
    }
    inserted: uuid.UUID | None = (
        await s.execute(
            text(
                "INSERT INTO renders (id, tenant_id, job_id, matter_id, options, options_hash,"
                " renderer_version, unicode_version, tzdata_version, requested_by, request_id,"
                " idempotency_key) VALUES (:id, :t, :j, :m, CAST(:o AS jsonb), :h, :rv, :uv, :tv, :by,"
                " :rid, :key)"
                " ON CONFLICT (tenant_id, job_id, options_hash, renderer_version, unicode_version,"
                " tzdata_version) WHERE status NOT IN ('failed', 'refused') DO NOTHING RETURNING id"
            ),
            {
                **identity,
                "id": new_id(),
                "m": matter_id,
                "o": json.dumps(options.as_payload()),
                "by": requested_by,
                "rid": request_id,
                "key": idempotency_key,
            },
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return CreatedRender(inserted, True)
    existing: uuid.UUID = (
        await s.execute(
            text(
                "SELECT id FROM renders WHERE tenant_id = :t AND job_id = :j AND options_hash = :h"
                " AND renderer_version = :rv AND unicode_version = :uv AND tzdata_version = :tv"
                " AND status NOT IN ('failed', 'refused')"
            ),
            identity,
        )
    ).scalar_one()
    return CreatedRender(existing, False)


async def open_episode(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    render_id: uuid.UUID,
    job_id: uuid.UUID,
    kind: str,
    detail: str,
    message: str,
) -> bool:
    """Open an episode of ``kind`` unless one is open (partial unique index), with ONE alert per
    episode. Returns whether this call opened it."""
    opened = (
        await s.execute(
            text(
                "INSERT INTO production_episodes (id, tenant_id, render_id, subject_id, kind, detail)"
                " VALUES (:i, :t, :r, :r, :k, :d)"
                " ON CONFLICT (subject_id, kind) WHERE ended_at IS NULL DO NOTHING RETURNING id"
            ),
            {"i": new_id(), "t": tenant_id, "r": render_id, "k": kind, "d": detail[:2000]},
        )
    ).first()
    if opened is None:
        return False
    await s.execute(
        text(
            "INSERT INTO alerts (id, tenant_id, kind, job_id, message) VALUES (:i, :t, :k, :j, :m)"
        ),
        {"i": new_id(), "t": tenant_id, "k": f"render_{kind}", "j": job_id, "m": message[:2000]},
    )
    return True


async def close_episodes(
    s: AsyncSession, render_id: uuid.UUID, reason: str, *, kind: str | None = None
) -> int:
    """Close the open episode(s) of a render (all kinds, or one); closed episodes are history."""
    result = await s.execute(
        text(
            "UPDATE production_episodes SET ended_at = now(), end_reason = :why WHERE subject_id = :r"
            " AND ended_at IS NULL AND (CAST(:k AS text) IS NULL OR kind = :k)"
        ),
        {"why": reason, "r": render_id, "k": kind},
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class SourceCheck:
    refusal: tuple[str, str] | None  # (reason, detail)
    reference: dict[str, Any] = field(default_factory=dict)


@dataclass
class RenderRun:
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    settings: Settings
    hooks: CrashHooks = field(default_factory=CrashHooks)
    # the versions this worker renders (its queue); only tests pass anything but the runtime's
    versions: Mapping[str, str] = field(default_factory=runtime_versions)

    # ------------------------------------------------------------------ helpers
    async def row(self, tenant_id: uuid.UUID, render_id: uuid.UUID) -> Any:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return (
                await s.execute(text("SELECT * FROM renders WHERE id = :r"), {"r": render_id})
            ).one()

    @staticmethod
    async def _locked(s: AsyncSession, render_id: uuid.UUID) -> Any:
        return (
            await s.execute(
                text("SELECT * FROM renders WHERE id = :r FOR NO KEY UPDATE"), {"r": render_id}
            )
        ).one()

    async def _append(
        self, s: AsyncSession, row: Any, event_type: str, payload: dict[str, Any], **kw: Any
    ) -> None:
        await append(
            s,
            tenant_id=row.tenant_id,
            stream_id=row.id,
            render_id=row.id,
            event_type=event_type,
            actor=ACTOR,
            payload=payload,
            anchor_every=self.settings.custody_anchor_every_n_batches,
            **kw,
        )

    async def _anchor(self, tenant_id: uuid.UUID, render_id: uuid.UUID) -> None:
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=render_id
        )

    def _check_versions(self, row: Any) -> None:
        """The safety net behind version routing: a render that reaches a worker of other versions
        (misrouted) fails rather than being rendered to bytes its identity does not promise."""
        mine = dict(self.versions)
        theirs = {k: getattr(row, k) for k in mine}
        if theirs != mine:
            raise RenderIntegrityError(
                f"render {row.id} was requested for {theirs}; this worker renders {mine}"
            )

    # ------------------------------------------------------------------ 1. begin
    async def begin(self, tenant_id: uuid.UUID, render_id: uuid.UUID) -> str:
        row = await self.row(tenant_id, render_id)
        if row.status != "requested":
            return str(row.status)
        self._check_versions(row)
        check = await self.check_source(tenant_id, row.job_id)
        await self.hooks.hit("begin_checked")
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, render_id)
            if cur.status != "requested":
                return str(cur.status)
            base = {
                "render_id": str(render_id),
                **self.versions,
                "options": dict(cur.options),
                "options_hash": cur.options_hash,
                "requested_by": cur.requested_by,
                **({"request_id": cur.request_id} if cur.request_id else {}),
                **({"idempotency_key": cur.idempotency_key} if cur.idempotency_key else {}),
            }
            await close_episodes(s, render_id, "picked_up", kind="unroutable")
            if check.refusal is not None:
                reason, detail = check.refusal
                await self._append(
                    s,
                    cur,
                    RENDER_REFUSED,
                    {**base, "job_id": str(cur.job_id), "reason": reason, "detail": detail},
                )
                await s.execute(
                    text(
                        "UPDATE renders SET status = 'refused', reason = :r, detail = :d,"
                        " finished_at = now(), updated_at = now() WHERE id = :i"
                    ),
                    {"r": reason, "d": detail, "i": render_id},
                )
                status = "refused"
            else:
                ref = check.reference
                await self._append(s, cur, RENDER_STARTED, {**base, "job": ref})
                await s.execute(
                    text(
                        "UPDATE renders SET status = 'rendering', job_head_seq = :seq, job_head_hash = :h,"
                        " job_seal_key = :k, job_seal_version = :v, started_at = now(), updated_at = now()"
                        " WHERE id = :i"
                    ),
                    {
                        "seq": ref["head"]["seq"],
                        "h": ref["head"]["hash"],
                        "k": ref["seal"]["key"],
                        "v": ref["seal"]["version_id"],
                        "i": render_id,
                    },
                )
                status = "rendering"
            await self.hooks.hit("begin_tx")
        await self.hooks.hit("begin_committed")
        await self._anchor(tenant_id, render_id)
        await self.hooks.hit("after_begin")
        return status

    async def check_source(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> SourceCheck:
        """Everything that must hold before a job is rendered. Read-only; the chain is verified
        outside any transaction (it reads every event)."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            job = (
                await s.execute(
                    text(
                        "SELECT j.status, j.sealed_at, j.finished_at, j.seal_storage_key,"
                        " m.closed_at AS matter_closed, c.closed_at AS client_closed,"
                        " cn.source AS connection_source FROM collection_jobs j"
                        " JOIN matters m ON m.id = j.matter_id JOIN clients c ON c.id = m.client_id"
                        " JOIN connections cn ON cn.id = j.connection_id WHERE j.id = :j"
                    ),
                    {"j": job_id},
                )
            ).one()
        if job.sealed_at is None or job.finished_at is None or job.seal_storage_key is None:
            return SourceCheck(("job_not_sealed", f"job {job_id} is {job.status} and not sealed"))
        if job.matter_closed is not None:
            return SourceCheck(("matter_closed", "the job's matter is closed"))
        if job.client_closed is not None:
            return SourceCheck(("client_closed", "the job's client is closed"))
        report = await verify_chain(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=job_id,
            require_seal=True,
        )  # fmt: skip
        if not report.ok:
            return SourceCheck(("chain_verification_failed", "; ".join(report.errors[:5])[:2000]))
        key = str(job.seal_storage_key)
        if key != anchor_key(str(tenant_id), str(job_id), report.events):
            return SourceCheck(
                ("seal_mismatch", f"seal {key} is not the anchor of head {report.events}")
            )
        versions = [
            v
            async for v in list_versions(
                self.s3, bucket=self.settings.s3_evidence_bucket, prefix=key
            )
            if v.key == key
        ]
        if len(versions) != 1 or versions[0].is_delete_marker:
            return SourceCheck(
                ("seal_mismatch", f"seal {key} has {len(versions)} versions, expected 1")
            )
        body = await get_bytes(
            self.s3,
            bucket=self.settings.s3_evidence_bucket,
            key=key,
            version_id=versions[0].version_id,
        )
        doc = json.loads(body)
        expected = {
            "format": ANCHOR_FORMAT,
            "tenant_id": str(tenant_id),
            "stream_id": str(job_id),
            "seq": report.events,
            "event_hash": report.head_hash,
        }
        if doc != expected:
            return SourceCheck(("seal_mismatch", f"seal {key} does not anchor the verified head"))
        basis = "archive" if job.connection_source == "slack_export" else "source"
        return SourceCheck(
            None,
            {
                "id": str(job_id),
                "status": job.status,
                "completeness_basis": basis,
                "head": {"seq": report.events, "hash": report.head_hash},
                "seal": {"key": key, "version_id": versions[0].version_id},
            },
        )

    # ------------------------------------------------------------------ 2. files
    async def render_files(self, tenant_id: uuid.UUID, render_id: uuid.UUID) -> str:
        row = await self.row(tenant_id, render_id)
        if row.status != "rendering":
            return str(row.status)
        self._check_versions(row)
        size = self.settings.render_files_batch_size
        pending: list[StoredFile] = []

        async def sink(f: StoredFile) -> None:
            pending.append(f)
            if len(pending) == size:
                await self._commit_batch(tenant_id, render_id, pending, size)
                pending.clear()

        try:
            out = await render_and_store(
                self.sessions, self.s3, self.settings, tenant_id=tenant_id, job_id=row.job_id,
                render_id=render_id, options=options_of(row), on_stored=sink, hooks=self.hooks,
            )  # fmt: skip
            if pending:
                await self._commit_batch(tenant_id, render_id, pending, size)
        except _Superseded:
            return str((await self.row(tenant_id, render_id)).status)
        summary = {**out.reconciliation.as_payload(), "verified_objects": out.verified_objects}
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, render_id)
            if cur.status != "rendering":
                return str(cur.status)
            roots = await self._batch_roots(s, render_id)
            natives = await self._native_records(s, render_id)
            # what the batches committed, against what the files reference (ADR 0015 §20.4)
            out.reconciler.check_natives((n["sha256"], n["size"]) for n in natives)
            if len(natives) != out.native_count:
                raise RenderIntegrityError(
                    f"render {render_id}: {out.native_count} natives written, {len(natives)} recorded"
                )
            if (cur.files_done, cur.batches_done, len(roots)) != (
                out.file_count,
                -(-out.file_count // size),
                cur.batches_done,
            ):
                raise RenderIntegrityError(
                    f"render {render_id}: {out.file_count} files rendered, {cur.files_done} recorded in"
                    f" {cur.batches_done} batches ({len(roots)} batch events)"
                )
            await s.execute(
                text(
                    "UPDATE renders SET status = 'rendered', file_count = :n, batches_root = :root,"
                    " native_count = :nn, natives_root = :nroot,"
                    " summary = CAST(:sum AS jsonb), updated_at = now() WHERE id = :i"
                ),
                {"n": out.file_count, "root": batches_root(roots), "sum": json.dumps(summary),
                 "nn": len(natives), "nroot": natives_root(natives), "i": render_id},
            )  # fmt: skip
            await self.hooks.hit("files_tx")
        await self.hooks.hit("after_files")
        return "rendered"

    @staticmethod
    async def _batch_roots(s: AsyncSession, render_id: uuid.UUID) -> list[str]:
        rows: Sequence[Any] = (
            (
                await s.execute(
                    text(
                        "SELECT payload->>'merkle_root' FROM custody_events WHERE stream_id = :r"
                        " AND event_type = :t ORDER BY seq"
                    ),
                    {"r": render_id, "t": RENDER_BATCH_EVENT},
                )
            )
            .scalars()
            .all()
        )
        return [str(r) for r in rows]

    @staticmethod
    async def _native_records(
        s: AsyncSession,
        render_id: uuid.UUID,
        event_id: uuid.UUID | None = None,
    ) -> list[dict[str, Any]]:
        """The render's native records in ord order (or one batch's), as their leaves see them."""
        rows = (
            await s.execute(
                text(
                    "SELECT ord, sha256, size_bytes, storage_key, version_id, file_ords"
                    " FROM render_natives WHERE render_id = :r"
                    " AND (CAST(:e AS uuid) IS NULL OR custody_event_id = :e) ORDER BY ord"
                ),
                {"r": render_id, "e": event_id},
            )
        ).all()
        return [native_record(r) for r in rows]

    @staticmethod
    def _leaf(f: StoredFile) -> dict[str, Any]:
        return {
            **f.record,
            "ord": f.ord,
            "version_id": f.version_id,
            "sha256": f.sha256,
            "size": f.size,
        }

    async def _commit_batch(
        self, tenant_id: uuid.UUID, render_id: uuid.UUID, files: Sequence[StoredFile], size: int
    ) -> None:
        first = files[0].ord
        index = first // size
        records = [self._leaf(f) for f in files]
        natives = [n.record() for f in files for n in f.natives]
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, render_id)
            if cur.status != "rendering":
                raise _Superseded()
            if index < cur.batches_done:
                # an earlier attempt committed this batch: the re-render must be exactly what it recorded
                recorded: Sequence[dict[str, Any]] = (
                    (
                        await s.execute(
                            text(
                                "SELECT record FROM render_files WHERE render_id = :r AND ord >= :a"
                                " AND ord < :b ORDER BY ord"
                            ),
                            {"r": render_id, "a": first, "b": first + size},
                        )
                    )
                    .scalars()
                    .all()
                )
                recorded_natives = (
                    await s.execute(
                        text(
                            "SELECT n.ord, n.sha256, n.size_bytes, n.storage_key, n.version_id,"
                            " n.file_ords FROM render_natives n JOIN render_files f"
                            " ON f.render_id = n.render_id AND f.custody_event_id = n.custody_event_id"
                            " WHERE n.render_id = :r AND f.ord = :a ORDER BY n.ord"
                        ),
                        {"r": render_id, "a": first},
                    )
                ).all()
                if [dict(r) for r in recorded] != records or [
                    native_record(n) for n in recorded_natives
                ] != natives:
                    raise RenderIntegrityError(
                        f"render {render_id}: batch {index} re-renders to other files than recorded"
                    )
                return
            if index != cur.batches_done or first != cur.files_done:
                raise RenderIntegrityError(
                    f"render {render_id}: batch {index} (ord {first}) does not follow batch"
                    f" {cur.batches_done - 1} ({cur.files_done} files recorded)"
                )
            event_id = new_id()
            await s.execute(
                text(
                    "INSERT INTO render_files (tenant_id, render_id, ord, name, evidence_object_id,"
                    " version_id, sha256, size_bytes, record, custody_event_id)"
                    " SELECT :t, :r, x.ord, x.name, x.ev, x.v, x.sha, x.size, x.rec, :e"
                    " FROM jsonb_to_recordset(CAST(:rows AS jsonb))"
                    " AS x(ord integer, name text, ev uuid, v text, sha text, size bigint, rec jsonb)"
                ),
                {
                    "t": tenant_id,
                    "r": render_id,
                    "e": event_id,
                    "rows": json.dumps(
                        [
                            {
                                "ord": f.ord,
                                "name": f.name,
                                "ev": str(f.evidence_id),
                                "v": f.version_id,
                                "sha": f.sha256,
                                "size": f.size,
                                "rec": rec,
                            }
                            for f, rec in zip(files, records, strict=True)
                        ]
                    ),
                },
            )
            first_native = int(
                (
                    await s.execute(
                        text("SELECT count(*) FROM render_natives WHERE render_id = :r"),
                        {"r": render_id},
                    )
                ).scalar_one()
            )
            if natives:
                if natives[0]["ord"] != first_native:
                    raise RenderIntegrityError(
                        f"render {render_id}: batch {index} natives start at {natives[0]['ord']},"
                        f" {first_native} are recorded"
                    )
                await s.execute(
                    text(
                        "INSERT INTO render_natives (tenant_id, render_id, ord, sha256, size_bytes,"
                        " storage_key, version_id, file_ords, evidence_object_id, custody_event_id)"
                        " SELECT :t, :r, x.ord, x.sha, x.size, x.key, x.v,"
                        " ARRAY(SELECT jsonb_array_elements_text(x.ords)::integer), x.ev, :e"
                        " FROM jsonb_to_recordset(CAST(:rows AS jsonb))"
                        " AS x(ord integer, sha text, size bigint, key text, v text, ords jsonb, ev uuid)"
                    ),
                    {
                        "t": tenant_id,
                        "r": render_id,
                        "e": event_id,
                        "rows": json.dumps(
                            [
                                {
                                    "ord": n.ord,
                                    "sha": n.sha256,
                                    "size": n.size,
                                    "key": n.storage_key,
                                    "v": n.version_id,
                                    "ords": list(n.file_ords),
                                    "ev": str(n.evidence_id),
                                }
                                for f in files
                                for n in f.natives
                            ]
                        ),
                    },
                )
                await self.hooks.hit("natives_inserted")
            await self._append(
                s,
                cur,
                RENDER_BATCH_EVENT,
                {
                    "render_id": str(render_id),
                    "batch": index,
                    "first_ord": first,
                    "file_count": len(files),
                    "merkle_root": files_root(records),
                    "native_count": len(natives),
                    "first_native_ord": first_native,
                    "natives_root": natives_root(natives, first_native),
                },
                event_id=event_id,
            )
            await s.execute(
                text(
                    "UPDATE renders SET batches_done = batches_done + 1, files_done = files_done + :n,"
                    " updated_at = now() WHERE id = :i"
                ),
                {"n": len(files), "i": render_id},
            )
            await self.hooks.hit("batch_tx")
        await self.hooks.hit("batch_committed")
        await self._anchor(tenant_id, render_id)
        await self.hooks.hit("after_batch")

    # ------------------------------------------------------------------ 3. complete
    async def complete(self, tenant_id: uuid.UUID, render_id: uuid.UUID) -> dict[str, Any]:
        row = await self.row(tenant_id, render_id)
        if row.status == "rendered":
            async with tenant_tx(self.sessions, tenant_id) as s:
                cur = await self._locked(s, render_id)
                if cur.status == "rendered":
                    summary = dict(cur.summary)
                    await self._append(
                        s,
                        cur,
                        RENDER_COMPLETED,
                        {
                            "render_id": str(render_id),
                            "job_id": str(cur.job_id),
                            "file_count": cur.file_count,
                            "batch_count": cur.batches_done,
                            "batches_root": cur.batches_root,
                            "native_count": cur.native_count,
                            "natives_root": cur.natives_root,
                            "verified_objects": summary.pop("verified_objects"),
                            "reconciliation": summary,
                        },
                    )
                    await s.execute(
                        text(
                            "UPDATE renders SET status = 'completed', finished_at = now(),"
                            " updated_at = now() WHERE id = :i"
                        ),
                        {"i": render_id},
                    )
                    await self.hooks.hit("complete_tx")
            await self.hooks.hit("complete_committed")
            await self._anchor(tenant_id, render_id)
            await self.hooks.hit("after_completed")
            row = await self.row(tenant_id, render_id)
        if row.status not in FINAL:
            raise RenderIntegrityError(f"render {render_id} is {row.status}: nothing to complete")
        return await self.seal(tenant_id, render_id)

    # ------------------------------------------------------------------ failure
    async def fail(
        self, tenant_id: uuid.UUID, render_id: uuid.UUID, error_type: str, error: str
    ) -> dict[str, Any]:
        row = await self.row(tenant_id, render_id)
        if row.status in LIVE:
            async with tenant_tx(self.sessions, tenant_id) as s:
                cur = await self._locked(s, render_id)
                if cur.status in LIVE:
                    await self._append(
                        s,
                        cur,
                        RENDER_FAILED,
                        {
                            "render_id": str(render_id),
                            "job_id": str(cur.job_id),
                            "from_status": cur.status,
                            "error_type": error_type[:200],
                            "error": error[:2000],
                            "files_done": cur.files_done,
                            "batches_done": cur.batches_done,
                        },
                    )
                    await close_episodes(s, render_id, "render_final", kind="unroutable")
                    await s.execute(
                        text(
                            "UPDATE renders SET status = 'failed', reason = :r, detail = :d,"
                            " finished_at = now(), updated_at = now() WHERE id = :i"
                        ),
                        {"r": error_type[:200] or "error", "d": error[:2000], "i": render_id},
                    )
                    await s.execute(
                        text(
                            "INSERT INTO alerts (id, tenant_id, kind, job_id, message)"
                            " VALUES (:i, :t, 'render_failed', :j, :m)"
                        ),
                        {"i": new_id(), "t": tenant_id, "j": cur.job_id,
                         "m": f"render {render_id} failed: {error_type}: {error}"[:2000]},
                    )  # fmt: skip
                    await self.hooks.hit("fail_tx")
            await self.hooks.hit("fail_committed")
            await self._anchor(tenant_id, render_id)
        return await self.seal(tenant_id, render_id)

    # ------------------------------------------------------------------ the seal
    async def seal(self, tenant_id: uuid.UUID, render_id: uuid.UUID) -> dict[str, Any]:
        """Anchor the final head (forced) and record it once on the render, with the tenant audit
        event in the same transaction, so a retry neither re-records nor re-audits.

        Retried without limit by the workflow. Every failed attempt is counted on the render; past
        ``render_seal_stuck_attempts`` failures, or ``render_seal_stuck_seconds`` after the render
        became final (checked at every attempt, so attempts killed before they could count are
        covered too), the render is flagged ``sealing_stuck`` once, with an alert."""
        row = await self.row(tenant_id, render_id)
        if row.status not in FINAL:
            raise RenderIntegrityError(f"render {render_id} is {row.status}: not final, not sealed")
        if row.seal_storage_key is None:
            await self._flag_if_stuck(tenant_id, render_id, None)
            try:
                await self._seal_once(tenant_id, row)
            except Exception as exc:
                try:
                    await self._flag_if_stuck(tenant_id, render_id, describe(exc))
                except Exception as bookkeeping:  # noqa: BLE001 - reported on the original, which is raised
                    exc.add_note(f"recording the failed seal attempt also failed: {bookkeeping!r}")
                raise
            row = await self.row(tenant_id, render_id)
        return {
            "status": row.status,
            "reason": row.reason,
            "file_count": row.file_count,
            "head_seq": row.head_seq,
            "seal_storage_key": row.seal_storage_key,
            "sealing_stuck": await self._open_episode(tenant_id, render_id, "sealing_stuck"),
        }

    async def _open_episode(self, tenant_id: uuid.UUID, render_id: uuid.UUID, kind: str) -> bool:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return (
                await s.execute(
                    text(
                        "SELECT 1 FROM production_episodes WHERE subject_id = :r AND kind = :k"
                        " AND ended_at IS NULL"
                    ),
                    {"r": render_id, "k": kind},
                )
            ).first() is not None

    async def _flag_if_stuck(
        self, tenant_id: uuid.UUID, render_id: uuid.UUID, error: str | None
    ) -> None:
        """Count a failed seal attempt (``error``), and open a ``sealing_stuck`` episode (one alert)
        when sealing is stuck and no episode is open. Only the seal closes it."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, render_id)
            if cur.seal_storage_key is not None:
                return
            failures = cur.seal_failures + (error is not None)
            if error is not None:
                await s.execute(
                    text(
                        "UPDATE renders SET seal_failures = :f, last_seal_error = :e, updated_at = now()"
                        " WHERE id = :i"
                    ),
                    {"f": failures, "e": error, "i": render_id},
                )
            overdue = bool(
                (
                    await s.execute(
                        text(
                            "SELECT now() - finished_at >= make_interval(secs => :t) FROM renders"
                            " WHERE id = :i"
                        ),
                        {"t": self.settings.render_seal_stuck_seconds, "i": render_id},
                    )
                ).scalar_one()
            )
            if failures >= self.settings.render_seal_stuck_attempts or overdue:
                await open_episode(
                    s, tenant_id=tenant_id, render_id=render_id, job_id=cur.job_id,
                    kind="sealing_stuck",
                    detail=f"{failures} failed attempt(s); last: {error or cur.last_seal_error}",
                    message=f"render {render_id} ({cur.status}) is not sealed after {failures} failed"
                    f" attempt(s): {error or cur.last_seal_error or 'no error recorded'}",
                )  # fmt: skip

    async def _seal_once(self, tenant_id: uuid.UUID, row: Any) -> None:
        render_id = row.id
        await self.hooks.hit("seal_start")
        key = await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=render_id,
            force=True,
        )  # fmt: skip
        await self.hooks.hit("after_seal_anchor")
        async with tenant_tx(self.sessions, tenant_id) as s:
            head = (
                await s.execute(
                    text(
                        "SELECT last_seq, last_hash FROM custody_chain_heads WHERE stream_id = :r"
                    ),
                    {"r": render_id},
                )
            ).one()
            if key != anchor_key(str(tenant_id), str(render_id), head.last_seq):
                raise RenderIntegrityError(f"render {render_id}: seal {key} is not the head anchor")
            version: str = (
                await s.execute(
                    text(
                        "SELECT version_id FROM evidence_objects WHERE storage_key = :k"
                        " AND state = 'complete' AND render_id = :r"
                    ),
                    {"k": key, "r": render_id},
                )
            ).scalar_one()
            sealed = (
                await s.execute(
                    text(
                        "UPDATE renders SET seal_storage_key = :k, seal_version_id = :v, head_seq = :seq,"
                        " head_hash = :h, sealed_at = now(), updated_at = now()"
                        " WHERE id = :i AND seal_storage_key IS NULL RETURNING id"
                    ),
                    {
                        "k": key,
                        "v": version,
                        "seq": head.last_seq,
                        "h": head.last_hash,
                        "i": render_id,
                    },
                )
            ).first()
            if sealed is not None:
                await close_episodes(s, render_id, "sealed")
                await append(
                    s,
                    tenant_id=tenant_id,
                    stream_id=tenant_id,
                    event_type=f"audit.render_{row.status}",
                    actor=ACTOR,
                    payload={
                        "render_id": str(render_id),
                        "job_id": str(row.job_id),
                        "matter_id": str(row.matter_id),
                        "status": row.status,
                        **({"reason": row.reason} if row.reason else {}),
                        **({"file_count": row.file_count} if row.file_count is not None else {}),
                        "head": {"seq": head.last_seq, "hash": head.last_hash},
                        "seal": {"key": key, "version_id": version},
                    },
                )
            await self.hooks.hit("seal_tx")
        await self.hooks.hit("sealed")
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=tenant_id
        )


class BarrierHooks(CrashHooks):
    """TEST ONLY (``EDISC_TEST_RENDER_BARRIER``, refused outside test/ci by Settings): at one crash
    point, announce it (``<dir>/<point>.reached``) and block, so a test can SIGKILL the worker
    process exactly there. An async point (``hit``) is never released: the process is meant to die
    waiting. A synchronous point (``block``, inside CPU-bound work that runs in a worker thread)
    spins in that thread, like the work it stands for, until the test writes
    ``<dir>/<point>.release``, once; later passes go through."""

    def __init__(self, spec: str) -> None:
        point, _, directory = spec.partition(":")
        if not point or not directory:
            raise ValueError(f"EDISC_TEST_RENDER_BARRIER must be '<point>:<dir>', got {spec!r}")
        self.point, self.directory = point, Path(directory)
        self._released = False

    async def hit(self, point: str) -> None:
        if point != self.point:
            return
        (self.directory / f"{point}.reached").write_text(str(os.getpid()))
        await asyncio.Event().wait()  # never set: blocks until the process is killed

    def block(self, point: str) -> None:
        if point != self.point or self._released:
            return
        (self.directory / f"{point}.reached").write_text(str(os.getpid()))
        release = self.directory / f"{point}.release"
        while not release.exists():  # spin like CPU-bound rendering (pure Python, holds the GIL)
            deadline = time.monotonic() + 0.05
            while time.monotonic() < deadline:
                pass
        self._released = True


def barrier_hooks(settings: Settings) -> CrashHooks:
    return (
        BarrierHooks(settings.test_render_barrier) if settings.test_render_barrier else CrashHooks()
    )


# ------------------------------------------------------------------ activities
_RENDER_INTEGRITY = (
    RenderIntegrityError,
    RenderInputIntegrityError,
    RenderRefusedError,
    EvidenceIntegrityError,
    RenderError,  # renderer errors: mismatched evidence, reconciliation, invalid manifest, zip limits
    RenderFileError,
)


def render_error_class(exc: BaseException) -> ErrorClass:
    if isinstance(exc, _RENDER_INTEGRITY):
        return ErrorClass.RENDER_INTEGRITY
    return classify(exc)


def _classified[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Every exception leaves as a classified ApplicationError (ADR 0012 section 3): render integrity
    problems are final, transient ones are retried by the workflow's policy."""

    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            raise to_application_error(exc, error_class=render_error_class(exc)) from exc

    return wrapper


@dataclass
class RenderActivities:
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    settings: Settings
    hooks: CrashHooks = field(default_factory=CrashHooks)
    versions: Mapping[str, str] = field(default_factory=runtime_versions)

    @property
    def task_queue(self) -> str:
        return render_task_queue(**self.versions)

    def _run(self) -> RenderRun:
        return RenderRun(self.sessions, self.s3, self.settings, self.hooks, self.versions)

    @activity.defn(name="begin_render")
    @_ticking
    @_classified
    async def begin_render(self, ref: RenderRef) -> str:
        return await self._run().begin(uuid.UUID(ref.tenant_id), uuid.UUID(ref.render_id))

    @activity.defn(name="render_files")
    @_ticking
    @_classified
    async def render_files(self, ref: RenderRef) -> str:
        return await self._run().render_files(uuid.UUID(ref.tenant_id), uuid.UUID(ref.render_id))

    @activity.defn(name="complete_render")
    @_ticking
    @_classified
    async def complete_render(self, ref: RenderRef) -> dict[str, Any]:
        return await self._run().complete(uuid.UUID(ref.tenant_id), uuid.UUID(ref.render_id))

    @activity.defn(name="fail_render")
    @_ticking
    @_classified
    async def fail_render(self, failure: RenderFailure) -> dict[str, Any]:
        ref = failure.render
        return await self._run().fail(
            uuid.UUID(ref.tenant_id), uuid.UUID(ref.render_id), failure.error_type, failure.error
        )

    def all(self) -> list[Any]:
        return [self.begin_render, self.render_files, self.complete_render, self.fail_render]


async def start_render_workflow(
    client: Client,
    settings: Settings,
    tenant_id: uuid.UUID,
    render_id: uuid.UUID,
    versions: Mapping[str, str],
) -> None:
    """Start a render's workflow once, on the queue of the versions it recorded (ADR 0015 §15). A
    replayed request after a crash between commit and start starts it; "already started" is fine."""
    from edisc_worker.workflows import RenderWorkflow

    try:
        await client.start_workflow(
            RenderWorkflow.run,
            RenderRef.from_settings(str(tenant_id), str(render_id), settings),
            id=render_workflow_id(str(render_id)),
            task_queue=render_task_queue(
                versions["renderer_version"],
                versions["unicode_version"],
                versions["tzdata_version"],
            ),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
    except WorkflowAlreadyStartedError:
        return
