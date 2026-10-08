"""Collection reports of sealed jobs: the report's own custody stream and the activities of
``ReportWorkflow`` (ADR 0018 §9, §10, §11, §13; M16 step 4).

A report moves ``requested -> snapshotted -> generating -> generated -> completed``, or ends
``refused`` (the job is not sealed: ``report_refused`` is then the only event) or ``failed``. Every
step is one activity, idempotent from the database alone: it acts only if the report is still in the
status the step starts from, and moves the status in the SAME transaction as the custody event it
appends (the status is the fence, never a value that can repeat).

- ``snapshot``: every mutable input captured once (renders sealed now, retention gaps, the bucket's
  lock settings, the tenant audit head) into ``reports.snapshot`` (write-once) with its digest.
- ``begin``: the job chain verified with the seal required and the seal listed from S3, then
  ``report_started``: the job reference (id, status, basis, final head, seal key + VersionId), the
  snapshot digest, the identity (renderer, PDF toolchain id, Unicode, paper), the image digest, the
  requester and, for manual requests, the reason. A chain that fails verification still gets a
  report, which leads with the failure.
- ``files``: `report.json` and the JSONL files built by `ReportLoader` and stored as locked evidence
  (kind ``report``, `t/{tenant}/reports/{report}/{name}`), each recorded in ``report_files`` when
  complete (``files_done`` is the fence); then ``report_generated`` (every file record, the RFC 6962
  root over them, the clean verdict, the divergence count) in the transaction that moves the report
  to ``generated``. A retry rebuilds the same bytes (the snapshot is fixed) and dedups.
- ``seal``: a forced anchor of the final head, then in ONE transaction the seal on the report,
  ``completed`` (or the final status it already has), ``audit.report_completed`` (``_failed`` /
  ``_refused``) and the closing of the job's ``report_missing`` episode.
- ``fail``: ``report_failed`` with the error, an alert, then the same seal. Retried without limit.

Report events have ``report_id`` set and ``job_id`` NULL: a sealed job's chain is never appended to.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.chain import anchor_key
from edisc_custody.log import anchor_if_due, append
from edisc_custody.report_files import (
    REPORT_FAILED,
    REPORT_GENERATED,
    REPORT_REFUSED,
    REPORT_STARTED,
    file_record,
    files_root,
)
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceIntegrityError, EvidenceWriter
from edisc_renderers.report.version import REPORT_RENDERER_VERSION
from edisc_worker.activities import _ticking
from edisc_worker.contracts import (
    ErrorClass,
    ReportFailure,
    ReportRef,
    report_task_queue,
    report_workflow_id,
)
from edisc_worker.errors import classify, describe, to_application_error
from edisc_worker.pipeline import CrashHooks
from edisc_worker.report_loader import ReportLoader

ACTOR = "system:report"
LIVE = ("requested", "snapshotted", "generating", "generated")
FINAL = ("completed", "refused", "failed")
DEFAULT_PAPER = "letter"  # pending mentor (ADR 0018 §17.1): one constant, part of the identity
NO_TOOLCHAIN = "none"  # the PDF toolchain id until the PDF exists (M16 step 3)

MEDIA_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".html": "text/html; charset=utf-8",
}


class ReportIntegrityError(RuntimeError):
    """What a report stored or recorded disagrees with what it builds now. Always an incident."""


def runtime_identity() -> dict[str, str]:
    """The report runtime this worker produces (ADR 0018 §6): renderer, PDF toolchain, Unicode."""
    import unicodedata

    return {
        "renderer_version": REPORT_RENDERER_VERSION,
        "toolchain_id": NO_TOOLCHAIN,
        "unicode_version": unicodedata.unidata_version,
    }


@dataclass(frozen=True)
class CreatedReport:
    report_id: uuid.UUID
    created: bool


async def create_report(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    matter_id: uuid.UUID,
    requested_by: str,
    paper: str = DEFAULT_PAPER,
    request_reason: str | None = None,
    request_id: str | None = None,
    identity: Mapping[str, str] | None = None,
) -> CreatedReport:
    """A new report in ``requested`` (inside the caller's tenant transaction). Its identity is
    complete only once the snapshot is taken (``snapshot``)."""
    ident = dict(identity or runtime_identity())
    report_id = new_id()
    await s.execute(
        text(
            "INSERT INTO reports (id, tenant_id, job_id, matter_id, renderer_version,"
            " unicode_version, toolchain_id, paper, requested_by, request_reason, request_id)"
            " VALUES (:i, :t, :j, :m, :rv, :uv, :tc, :p, :by, :why, :rid)"
        ),
        {
            "i": report_id,
            "t": tenant_id,
            "j": job_id,
            "m": matter_id,
            "rv": ident["renderer_version"],
            "uv": ident["unicode_version"],
            "tc": ident["toolchain_id"],
            "p": paper,
            "by": requested_by,
            "why": request_reason,
            "rid": request_id,
        },
    )
    return CreatedReport(report_id, True)


async def open_episode(
    s: AsyncSession, *, tenant_id: uuid.UUID, subject: tuple[str, uuid.UUID], kind: str,
    detail: str, message: str, job_id: uuid.UUID | None,
) -> bool:  # fmt: skip
    """Open a production episode of ``kind`` about (``report_id`` | ``job_id``, id) unless one is
    open, with ONE alert per episode. Returns whether this call opened it."""
    column, subject_id = subject
    if column not in ("report_id", "job_id"):
        raise ValueError(column)
    opened = (
        await s.execute(
            text(
                f"INSERT INTO production_episodes (id, tenant_id, {column}, subject_id, kind, detail)"  # noqa: S608
                " VALUES (:i, :t, :s, :s, :k, :d)"
                " ON CONFLICT (subject_id, kind) WHERE ended_at IS NULL DO NOTHING RETURNING id"
            ),
            {"i": new_id(), "t": tenant_id, "s": subject_id, "k": kind, "d": detail[:2000]},
        )
    ).first()
    if opened is None:
        return False
    await s.execute(
        text(
            "INSERT INTO alerts (id, tenant_id, kind, job_id, message) VALUES (:i, :t, :k, :j, :m)"
        ),
        {
            "i": new_id(),
            "t": tenant_id,
            "k": kind if kind.startswith("report_") else f"report_{kind}",
            "j": job_id,
            "m": message[:2000],
        },
    )
    return True


async def close_episodes(
    s: AsyncSession, subject_id: uuid.UUID, reason: str, *, kind: str | None = None
) -> int:
    result = await s.execute(
        text(
            "UPDATE production_episodes SET ended_at = now(), end_reason = :why"
            " WHERE subject_id = :s AND ended_at IS NULL AND (CAST(:k AS text) IS NULL OR kind = :k)"
        ),
        {"why": reason, "s": subject_id, "k": kind},
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


@dataclass
class ReportRun:
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    settings: Settings
    hooks: CrashHooks = field(default_factory=CrashHooks)
    identity: Mapping[str, str] = field(default_factory=runtime_identity)
    image_digest: str | None = None

    # ------------------------------------------------------------------ helpers
    async def row(self, tenant_id: uuid.UUID, report_id: uuid.UUID) -> Any:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return (
                await s.execute(text("SELECT * FROM reports WHERE id = :r"), {"r": report_id})
            ).one()

    @staticmethod
    async def _locked(s: AsyncSession, report_id: uuid.UUID) -> Any:
        return (
            await s.execute(
                text("SELECT * FROM reports WHERE id = :r FOR NO KEY UPDATE"), {"r": report_id}
            )
        ).one()

    async def _append(
        self, s: AsyncSession, row: Any, event_type: str, payload: dict[str, Any]
    ) -> None:
        await append(
            s,
            tenant_id=row.tenant_id,
            stream_id=row.id,
            report_id=row.id,
            event_type=event_type,
            actor=ACTOR,
            payload=payload,
            anchor_every=self.settings.custody_anchor_every_n_batches,
        )

    async def _anchor(self, tenant_id: uuid.UUID, report_id: uuid.UUID) -> None:
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=report_id
        )

    def _check_identity(self, row: Any) -> None:
        """The safety net behind routing: a report that reaches a worker of another runtime fails
        rather than being built to bytes its identity does not promise (ADR 0018 §5.3)."""
        mine = dict(self.identity)
        theirs = {k: getattr(row, k) for k in mine}
        if theirs != mine:
            raise ReportIntegrityError(
                f"report {row.id} was requested for {theirs}; this worker produces {mine}"
            )

    def _loader(self, row: Any) -> ReportLoader:
        return ReportLoader(
            self.sessions, self.s3, self.settings, tenant_id=row.tenant_id, job_id=row.job_id
        )

    def _identity_payload(self, row: Any) -> dict[str, Any]:
        return {
            "renderer_version": row.renderer_version,
            "toolchain_id": row.toolchain_id,
            "unicode_version": row.unicode_version,
            "paper": row.paper,
        }

    # ------------------------------------------------------------------ 1. snapshot
    async def snapshot(self, tenant_id: uuid.UUID, report_id: uuid.UUID) -> str:
        row = await self.row(tenant_id, report_id)
        if row.status != "requested":
            return str(row.status)
        self._check_identity(row)
        async with tenant_tx(self.sessions, tenant_id) as s:
            job = (
                await s.execute(
                    text(
                        "SELECT status, sealed_at, seal_storage_key FROM collection_jobs"
                        " WHERE id = :j"
                    ),
                    {"j": row.job_id},
                )
            ).one()
        if job.sealed_at is None or job.seal_storage_key is None:
            return await self._refuse(
                tenant_id, report_id, "job_not_sealed", f"job {row.job_id} is {job.status}"
            )
        snap = await self._loader(row).snapshot()
        await self.hooks.hit("snapshot_taken")
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, report_id)
            if cur.status != "requested":
                return str(cur.status)
            try:
                async with s.begin_nested():
                    await s.execute(
                        text(
                            "UPDATE reports SET status = 'snapshotted', snapshot = CAST(:snap AS jsonb),"
                            " snapshot_digest = :d, snapshotted_at = now(), updated_at = now()"
                            " WHERE id = :i"
                        ),
                        {"snap": json.dumps(snap), "d": snap["digest"], "i": report_id},
                    )
            except IntegrityError:  # a live report of exactly this identity exists already
                other: Any = (
                    await s.execute(
                        text(
                            "SELECT id FROM reports WHERE tenant_id = :t AND job_id = :j"
                            " AND snapshot_digest = :d AND renderer_version = :rv"
                            " AND toolchain_id = :tc AND unicode_version = :uv AND paper = :p"
                            " AND status NOT IN ('requested', 'failed', 'refused')"
                        ),
                        {
                            "t": tenant_id,
                            "j": cur.job_id,
                            "d": snap["digest"],
                            "rv": cur.renderer_version,
                            "tc": cur.toolchain_id,
                            "uv": cur.unicode_version,
                            "p": cur.paper,
                        },
                    )
                ).scalar_one()
                await self._refuse_in(s, cur, "duplicate_identity", f"report {other} has it")
                return "refused"
            await self.hooks.hit("snapshot_tx")
        await self.hooks.hit("after_snapshot")
        return "snapshotted"

    async def _refuse(
        self, tenant_id: uuid.UUID, report_id: uuid.UUID, reason: str, detail: str
    ) -> str:
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, report_id)
            if cur.status != "requested":
                return str(cur.status)
            await self._refuse_in(s, cur, reason, detail)
            await self.hooks.hit("refuse_tx")
        await self._anchor(tenant_id, report_id)
        return "refused"

    async def _refuse_in(self, s: AsyncSession, cur: Any, reason: str, detail: str) -> None:
        await self._append(
            s,
            cur,
            REPORT_REFUSED,
            {
                "report_id": str(cur.id),
                "job_id": str(cur.job_id),
                "reason": reason,
                "detail": detail[:2000],
                "identity": self._identity_payload(cur),
                "requested_by": cur.requested_by,
            },
        )
        await s.execute(
            text(
                "UPDATE reports SET status = 'refused', reason = :r, detail = :d,"
                " finished_at = now(), updated_at = now() WHERE id = :i"
            ),
            {"r": reason, "d": detail[:2000], "i": cur.id},
        )

    # ------------------------------------------------------------------ 2. begin
    async def begin(self, tenant_id: uuid.UUID, report_id: uuid.UUID) -> str:
        row = await self.row(tenant_id, report_id)
        if row.status != "snapshotted":
            return str(row.status)
        self._check_identity(row)
        loader = self._loader(row)
        job = await loader.job()
        verification = await loader.verification(job)
        await self.hooks.hit("begin_verified")
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, report_id)
            if cur.status != "snapshotted":
                return str(cur.status)
            await close_episodes(s, report_id, "picked_up", kind="unroutable")
            basis = "archive" if job["connection_source"] == "slack_export" else "source"
            ref = {
                "id": str(cur.job_id),
                "status": job["status"],
                "completeness_basis": basis,
                "head": {"seq": verification["events"], "hash": verification["head_hash"]},
                "seal": {
                    "key": verification["seal_key"],
                    "version_id": verification["seal_version_id"],
                },
                "verified": verification["ok"],
                "verification_errors": verification["errors"][:20],
            }
            await self._append(
                s,
                cur,
                REPORT_STARTED,
                {
                    "report_id": str(report_id),
                    "job": ref,
                    "snapshot_digest": cur.snapshot_digest,
                    "identity": self._identity_payload(cur),
                    "image_digest": self.image_digest,
                    "requested_by": cur.requested_by,
                    **({"reason": cur.request_reason} if cur.request_reason else {}),
                    **({"request_id": cur.request_id} if cur.request_id else {}),
                },
            )
            await s.execute(
                text(
                    "UPDATE reports SET status = 'generating', job_head_seq = :seq, job_head_hash = :h,"
                    " job_seal_key = :k, job_seal_version = :v, image_digest = :img,"
                    " started_at = now(), updated_at = now() WHERE id = :i"
                ),
                {
                    "seq": ref["head"]["seq"],
                    "h": ref["head"]["hash"],
                    "k": ref["seal"]["key"],
                    "v": ref["seal"]["version_id"],
                    "img": self.image_digest,
                    "i": report_id,
                },
            )
            await self.hooks.hit("begin_tx")
        await self.hooks.hit("begin_committed")
        await self._anchor(tenant_id, report_id)
        return "generating"

    # ------------------------------------------------------------------ 3. files
    async def files(self, tenant_id: uuid.UUID, report_id: uuid.UUID) -> str:
        row = await self.row(tenant_id, report_id)
        if row.status != "generating":
            return str(row.status)
        self._check_identity(row)
        writer = EvidenceWriter(self.sessions, self.s3, self.settings)
        stored: list[dict[str, Any]] = []
        retention = await self._retention(tenant_id, row.job_id)

        async def sink(
            name: str, make: Callable[[], AsyncIterator[bytes]], rows: Callable[[], int | None]
        ) -> None:
            ord_ = len(stored)
            await self.hooks.hit(f"file:{name}")
            written = await writer.write_report_file(
                tenant_id=tenant_id,
                job_id=row.job_id,
                report_id=report_id,
                name=name,
                matter_retention_until=retention,
                stream=lambda: self._hooked(make()),
            )
            await self.hooks.hit("file_stored")
            record = {
                "ord": ord_,
                "name": name,
                "media_type": _media_type(name),
                "sha256": written.sha256,
                "size": written.size,
                "rows": rows(),
                "version_id": written.version_id,
            }
            await self._record_file(tenant_id, report_id, record, written.evidence_id)
            stored.append(record)
            await self.hooks.hit("file_recorded")

        built = await self._loader(row).build_files(
            sink,
            snapshot=dict(row.snapshot),
            identity=self._identity_payload(row),
            image_digest=row.image_digest,
        )
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, report_id)
            if cur.status != "generating":
                return str(cur.status)
            recorded = await self._file_records(s, report_id)
            if recorded != stored or cur.files_done != len(stored):
                raise ReportIntegrityError(
                    f"report {report_id}: {len(stored)} files built, {cur.files_done} recorded"
                )
            root = files_root(recorded)
            await self._append(
                s,
                cur,
                REPORT_GENERATED,
                {
                    "report_id": str(report_id),
                    "job_id": str(cur.job_id),
                    "files": recorded,
                    "files_root": root,
                    "clean": built.clean,
                    "divergences": len(built.divergences),
                },
            )
            await s.execute(
                text(
                    "UPDATE reports SET status = 'generated', files_root = :root, clean = :c,"
                    " divergence_count = :d, updated_at = now() WHERE id = :i"
                ),
                {"root": root, "c": built.clean, "d": len(built.divergences), "i": report_id},
            )
            if built.divergences:
                await self._divergence_alert(s, cur, len(built.divergences))
            await self.hooks.hit("generated_tx")
        await self.hooks.hit("generated_committed")
        await self._anchor(tenant_id, report_id)
        return "generated"

    async def _hooked(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        first = True
        async for chunk in chunks:
            yield chunk
            if first:
                first = False
                await self.hooks.hit("mid_upload")

    async def _retention(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> Any:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return (
                await s.execute(
                    text(
                        "SELECT m.retention_until FROM collection_jobs j JOIN matters m"
                        " ON m.id = j.matter_id WHERE j.id = :j"
                    ),
                    {"j": job_id},
                )
            ).scalar_one()

    async def _record_file(
        self, tenant_id: uuid.UUID, report_id: uuid.UUID, record: dict[str, Any],
        evidence_id: uuid.UUID,
    ) -> None:  # fmt: skip
        """Record a stored file, fenced by ``files_done``: an earlier attempt that recorded this
        ord must have recorded exactly this file, else it is an integrity incident."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, report_id)
            if cur.status != "generating":
                raise ReportIntegrityError(f"report {report_id} moved on to {cur.status}")
            if record["ord"] < cur.files_done:
                existing = (
                    await s.execute(
                        text(
                            "SELECT ord, name, media_type, sha256, size_bytes AS size, rows,"
                            " version_id FROM report_files WHERE report_id = :r AND ord = :o"
                        ),
                        {"r": report_id, "o": record["ord"]},
                    )
                ).one()
                if file_record(dict(existing._mapping)) != record:
                    raise ReportIntegrityError(
                        f"report {report_id}: {record['name']} rebuilds to other bytes than recorded"
                    )
                return
            if record["ord"] != cur.files_done:
                raise ReportIntegrityError(
                    f"report {report_id}: file {record['ord']} does not follow {cur.files_done}"
                )
            await s.execute(
                text(
                    "INSERT INTO report_files (tenant_id, report_id, ord, name, media_type,"
                    " evidence_object_id, version_id, sha256, size_bytes, rows)"
                    " VALUES (:t, :r, :o, :n, :mt, :ev, :v, :sha, :size, :rows)"
                ),
                {
                    "t": tenant_id,
                    "r": report_id,
                    "o": record["ord"],
                    "n": record["name"],
                    "mt": record["media_type"],
                    "ev": evidence_id,
                    "v": record["version_id"],
                    "sha": record["sha256"],
                    "size": record["size"],
                    "rows": record["rows"],
                },
            )
            await s.execute(
                text(
                    "UPDATE reports SET files_done = files_done + 1, updated_at = now()"
                    " WHERE id = :i"
                ),
                {"i": report_id},
            )
            await self.hooks.hit("file_tx")

    @staticmethod
    async def _file_records(s: AsyncSession, report_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            await s.execute(
                text(
                    "SELECT ord, name, media_type, sha256, size_bytes AS size, rows, version_id"
                    " FROM report_files WHERE report_id = :r ORDER BY ord"
                ),
                {"r": report_id},
            )
        ).all()
        return [file_record(dict(r._mapping)) for r in rows]

    async def _divergence_alert(self, s: AsyncSession, cur: Any, count: int) -> None:
        await s.execute(
            text(
                "INSERT INTO alerts (id, tenant_id, kind, job_id, message)"
                " VALUES (:i, :t, 'report_divergence', :j, :m)"
            ),
            {
                "i": new_id(),
                "t": cur.tenant_id,
                "j": cur.job_id,
                "m": f"report {cur.id}: {count} divergence(s) between the job chain and the database",
            },
        )
        await append(
            s,
            tenant_id=cur.tenant_id,
            stream_id=cur.tenant_id,
            event_type="audit.report_divergence",
            actor=ACTOR,
            payload={"report_id": str(cur.id), "job_id": str(cur.job_id), "divergences": count},
        )

    # ------------------------------------------------------------------ 4. complete / fail
    async def complete(self, tenant_id: uuid.UUID, report_id: uuid.UUID) -> dict[str, Any]:
        row = await self.row(tenant_id, report_id)
        if row.status not in ("generated", *FINAL):
            raise ReportIntegrityError(f"report {report_id} is {row.status}: nothing to complete")
        return await self.seal(tenant_id, report_id)

    async def fail(
        self, tenant_id: uuid.UUID, report_id: uuid.UUID, error_type: str, error: str
    ) -> dict[str, Any]:
        row = await self.row(tenant_id, report_id)
        if row.status in LIVE:
            async with tenant_tx(self.sessions, tenant_id) as s:
                cur = await self._locked(s, report_id)
                if cur.status in LIVE:
                    await self._append(
                        s,
                        cur,
                        REPORT_FAILED,
                        {
                            "report_id": str(report_id),
                            "job_id": str(cur.job_id),
                            "from_status": cur.status,
                            "error_type": error_type[:200],
                            "error": error[:2000],
                            "files_done": cur.files_done,
                        },
                    )
                    await close_episodes(s, report_id, "report_final", kind="unroutable")
                    await s.execute(
                        text(
                            "UPDATE reports SET status = 'failed', reason = :r, detail = :d,"
                            " finished_at = now(), updated_at = now() WHERE id = :i"
                        ),
                        {"r": error_type[:200] or "error", "d": error[:2000], "i": report_id},
                    )
                    await s.execute(
                        text(
                            "INSERT INTO alerts (id, tenant_id, kind, job_id, message)"
                            " VALUES (:i, :t, 'report_failed', :j, :m)"
                        ),
                        {
                            "i": new_id(),
                            "t": tenant_id,
                            "j": cur.job_id,
                            "m": f"report {report_id} failed: {error_type}: {error}"[:2000],
                        },
                    )
                    await self.hooks.hit("fail_tx")
            await self.hooks.hit("fail_committed")
            await self._anchor(tenant_id, report_id)
        return await self.seal(tenant_id, report_id)

    # ------------------------------------------------------------------ the seal
    async def seal(self, tenant_id: uuid.UUID, report_id: uuid.UUID) -> dict[str, Any]:
        """Anchor the final head (forced), then record the seal once, with ``completed`` (from
        ``generated``), the tenant audit event and the closing of the job's ``report_missing``
        episode in the same transaction, so a retry neither re-records nor re-audits. A seal that
        keeps failing opens a ``sealing_stuck`` episode once (ADR 0015 §16 pattern)."""
        row = await self.row(tenant_id, report_id)
        if row.status not in ("generated", *FINAL):
            raise ReportIntegrityError(f"report {report_id} is {row.status}: not sealable")
        if row.seal_storage_key is None:
            await self._flag_if_stuck(tenant_id, report_id, None)
            try:
                await self._seal_once(tenant_id, row)
            except Exception as exc:
                try:
                    await self._flag_if_stuck(tenant_id, report_id, describe(exc))
                except Exception as bookkeeping:  # noqa: BLE001 - reported on the original, which is raised
                    exc.add_note(f"recording the failed seal attempt also failed: {bookkeeping!r}")
                raise
            row = await self.row(tenant_id, report_id)
        return {
            "status": row.status,
            "reason": row.reason,
            "clean": row.clean,
            "files": row.files_done,
            "head_seq": row.head_seq,
            "seal_storage_key": row.seal_storage_key,
        }

    async def _flag_if_stuck(
        self, tenant_id: uuid.UUID, report_id: uuid.UUID, error: str | None
    ) -> None:
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, report_id)
            if cur.seal_storage_key is not None:
                return
            failures = cur.seal_failures + (error is not None)
            if error is not None:
                await s.execute(
                    text(
                        "UPDATE reports SET seal_failures = :f, last_seal_error = :e,"
                        " updated_at = now() WHERE id = :i"
                    ),
                    {"f": failures, "e": error, "i": report_id},
                )
            if failures >= self.settings.render_seal_stuck_attempts:
                await open_episode(
                    s,
                    tenant_id=tenant_id,
                    subject=("report_id", report_id),
                    kind="sealing_stuck",
                    job_id=cur.job_id,
                    detail=f"{failures} failed attempt(s); last: {error or cur.last_seal_error}",
                    message=f"report {report_id} ({cur.status}) is not sealed after {failures}"
                    f" failed attempt(s): {error or cur.last_seal_error or 'no error recorded'}",
                )

    async def _seal_once(self, tenant_id: uuid.UUID, row: Any) -> None:
        report_id = row.id
        await self.hooks.hit("seal_start")
        key = await anchor_if_due(
            self.sessions,
            self.s3,
            self.settings,
            tenant_id=tenant_id,
            stream_id=report_id,
            force=True,
        )
        await self.hooks.hit("after_seal_anchor")
        async with tenant_tx(self.sessions, tenant_id) as s:
            cur = await self._locked(s, report_id)
            head = (
                await s.execute(
                    text(
                        "SELECT last_seq, last_hash FROM custody_chain_heads WHERE stream_id = :r"
                    ),
                    {"r": report_id},
                )
            ).one()
            if key != anchor_key(str(tenant_id), str(report_id), head.last_seq):
                raise ReportIntegrityError(f"report {report_id}: seal {key} is not the head anchor")
            version: str = (
                await s.execute(
                    text(
                        "SELECT version_id FROM evidence_objects WHERE storage_key = :k"
                        " AND state = 'complete' AND report_id = :r"
                    ),
                    {"k": key, "r": report_id},
                )
            ).scalar_one()
            final = "completed" if cur.status == "generated" else cur.status
            sealed = (
                await s.execute(
                    text(
                        "UPDATE reports SET status = :st, seal_storage_key = :k, seal_version_id = :v,"
                        " head_seq = :seq, head_hash = :h, sealed_at = now(),"
                        " finished_at = coalesce(finished_at, now()), updated_at = now()"
                        " WHERE id = :i AND seal_storage_key IS NULL RETURNING id"
                    ),
                    {
                        "st": final,
                        "k": key,
                        "v": version,
                        "seq": head.last_seq,
                        "h": head.last_hash,
                        "i": report_id,
                    },
                )
            ).first()
            if sealed is not None:
                await close_episodes(s, report_id, "sealed")
                if final == "completed":
                    await close_episodes(s, cur.job_id, "report_completed", kind="report_missing")
                await append(
                    s,
                    tenant_id=tenant_id,
                    stream_id=tenant_id,
                    event_type=f"audit.report_{final}",
                    actor=ACTOR,
                    payload={
                        "report_id": str(report_id),
                        "job_id": str(cur.job_id),
                        "matter_id": str(cur.matter_id),
                        "status": final,
                        **({"reason": cur.reason} if cur.reason else {}),
                        **({"clean": cur.clean} if cur.clean is not None else {}),
                        "head": {"seq": head.last_seq, "hash": head.last_hash},
                        "seal": {"key": key, "version_id": version},
                    },
                )
            await self.hooks.hit("seal_tx")
        await self.hooks.hit("sealed")
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=tenant_id
        )


def _media_type(name: str) -> str:
    for suffix, media in MEDIA_TYPES.items():
        if name.endswith(suffix):
            return media
    raise ValueError(f"no media type for {name!r}")


Sink = Callable[
    [str, Callable[[], AsyncIterator[bytes]], Callable[[], int | None]], Awaitable[None]
]


# ------------------------------------------------------------------ activities
_REPORT_INTEGRITY = (ReportIntegrityError, EvidenceIntegrityError)


def _classified[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Every exception leaves as a classified ApplicationError (ADR 0012 section 3): report
    integrity problems are final, transient ones are retried by the workflow's policy."""

    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            kind = (
                ErrorClass.REPORT_INTEGRITY if isinstance(exc, _REPORT_INTEGRITY) else classify(exc)
            )
            raise to_application_error(exc, error_class=kind) from exc

    return wrapper


def report_barrier_hooks(settings: Settings) -> CrashHooks:
    """TEST ONLY (``EDISC_TEST_REPORT_BARRIER``): the render barrier hooks, at report points."""
    from edisc_worker.renders import BarrierHooks

    return (
        BarrierHooks(settings.test_report_barrier) if settings.test_report_barrier else CrashHooks()
    )


@dataclass
class ReportActivities:
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    settings: Settings
    hooks: CrashHooks = field(default_factory=CrashHooks)
    identity: Mapping[str, str] = field(default_factory=runtime_identity)

    @property
    def task_queue(self) -> str:
        return report_task_queue(**self.identity)

    def _run(self) -> ReportRun:
        return ReportRun(
            self.sessions,
            self.s3,
            self.settings,
            self.hooks,
            self.identity,
            image_digest=os.environ.get("EDISC_IMAGE_DIGEST"),
        )

    @activity.defn(name="snapshot_report")
    @_ticking
    @_classified
    async def snapshot_report(self, ref: ReportRef) -> str:
        return await self._run().snapshot(uuid.UUID(ref.tenant_id), uuid.UUID(ref.report_id))

    @activity.defn(name="begin_report")
    @_ticking
    @_classified
    async def begin_report(self, ref: ReportRef) -> str:
        return await self._run().begin(uuid.UUID(ref.tenant_id), uuid.UUID(ref.report_id))

    @activity.defn(name="report_files")
    @_ticking
    @_classified
    async def report_files(self, ref: ReportRef) -> str:
        return await self._run().files(uuid.UUID(ref.tenant_id), uuid.UUID(ref.report_id))

    @activity.defn(name="complete_report")
    @_ticking
    @_classified
    async def complete_report(self, ref: ReportRef) -> dict[str, Any]:
        return await self._run().complete(uuid.UUID(ref.tenant_id), uuid.UUID(ref.report_id))

    @activity.defn(name="fail_report")
    @_ticking
    @_classified
    async def fail_report(self, failure: ReportFailure) -> dict[str, Any]:
        ref = failure.report
        return await self._run().fail(
            uuid.UUID(ref.tenant_id), uuid.UUID(ref.report_id), failure.error_type, failure.error
        )

    def all(self) -> list[Any]:
        return [
            self.snapshot_report,
            self.begin_report,
            self.report_files,
            self.complete_report,
            self.fail_report,
        ]


async def start_report_workflow(
    client: Client, settings: Settings, tenant_id: uuid.UUID, report_id: uuid.UUID,
    identity: Mapping[str, str],
) -> None:  # fmt: skip
    """Start a report's workflow once, on the queue of the runtime it recorded (ADR 0018 §6). A
    replayed request after a crash between commit and start starts it; "already started" is fine."""
    from edisc_worker.workflows import ReportWorkflow

    try:
        await client.start_workflow(
            ReportWorkflow.run,
            ReportRef.from_settings(str(tenant_id), str(report_id), settings),
            id=report_workflow_id(str(report_id)),
            task_queue=report_task_queue(
                identity["renderer_version"], identity["toolchain_id"], identity["unicode_version"]
            ),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
    except WorkflowAlreadyStartedError:
        return
