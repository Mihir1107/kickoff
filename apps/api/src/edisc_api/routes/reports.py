"""Collection reports (ADR 0018 §2, §9, §10, §27): list, request, read, download.

- **Read** (``report.read``: every role, the collector included): ``GET /v1/jobs/{id}/reports`` lists
  EVERY report of the job, all generations (earlier reports are never replaced or hidden), each with
  its identity, status, snapshot time, who requested it and, for a manual request, why;
  ``GET /v1/reports/{id}`` and ``GET /v1/reports/{id}/files``.
- **Request** ``POST /v1/jobs/{id}/reports`` (``report.create``: tenant admins and matter managers;
  the recent-sign-in hook of ADR 0016 §4): a manual regeneration with a required ``reason`` and an
  optional ``paper``. Sealed jobs of open matters only (409 otherwise). Who and why go into
  ``audit.report_requested`` and the report's ``report_started``. Always a new report (201): every
  request takes a new snapshot, and its own audit event moves the audit head the snapshot holds.
- **Download** a file (``GET /v1/reports/{id}/files/{name}/content``) or the report package
  (``GET /v1/reports/{id}/package``, ``edisc-report-package/1``): completed (sealed) reports only;
  the audit (``audit.report_read`` / ``audit.report_package_read``) committed AND anchored before the
  first byte; bytes read by pinned version and re-hashed as they stream; an exact ``Content-Length``;
  a mismatch aborts the response with ``audit.report_read_aborted`` /
  ``audit.report_package_aborted`` and an alert.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import Caller, require_recent_sign_in
from edisc_api.authz import P, Permission, Scope, authorize, perm
from edisc_api.downloads import Download, GuardedResponse, guarded, still_permitted
from edisc_api.error_codes import E
from edisc_api.errors import ApiError, RecordedText, conflict, not_found
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, page_of
from edisc_core.ids import new_id
from edisc_custody.report_export import (
    PackageIntegrityError,
    plan_report_package,
    report_package_members,
)
from edisc_custody.zipwriter import ZipSizeError, zip_stream
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_verify.report_package import REPORT_PACKAGE_FORMAT
from edisc_worker.reports import (
    DEFAULT_PAPER,
    create_report,
    new_report_identity,
    start_report_workflow,
)

router = APIRouter(prefix="/v1")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportIn(Strict):
    reason: str = Field(min_length=1, max_length=2000)  # why it is regenerated (recorded)
    paper: Literal["letter", "a4"] = DEFAULT_PAPER  # type: ignore[assignment]


class ReportOut(Strict):
    id: uuid.UUID
    job_id: uuid.UUID
    matter_id: uuid.UUID
    status: str
    reason: RecordedText | None  # why it was refused or failed (e.g. duplicate_identity)
    detail: RecordedText | None
    requested_by: str  # "system" for the automatic report, else the requesting principal
    request_reason: str | None  # why a person requested it (manual regeneration)
    renderer_version: str
    toolchain_id: str
    unicode_version: str
    glyph_set: str
    paper: str
    snapshot_digest: str | None
    snapshotted_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    clean: bool | None
    divergence_count: int | None
    files_done: int
    head_seq: int | None
    head_hash: str | None
    seal_storage_key: str | None
    seal_version_id: str | None
    sealed: bool


class ReportFileOut(Strict):
    ord: int
    name: str
    media_type: str
    sha256: str
    size: int
    rows: int | None
    version_id: str


def _out(row: Any) -> ReportOut:
    return ReportOut(
        id=row.id, job_id=row.job_id, matter_id=row.matter_id, status=row.status,
        reason=row.reason, detail=row.detail, requested_by=row.requested_by,
        request_reason=row.request_reason, renderer_version=row.renderer_version,
        toolchain_id=row.toolchain_id, unicode_version=row.unicode_version,
        glyph_set=row.glyph_set, paper=row.paper, snapshot_digest=row.snapshot_digest,
        snapshotted_at=row.snapshotted_at, created_at=row.created_at, started_at=row.started_at,
        finished_at=row.finished_at, clean=row.clean, divergence_count=row.divergence_count,
        files_done=row.files_done, head_seq=row.head_seq, head_hash=row.head_hash,
        seal_storage_key=row.seal_storage_key, seal_version_id=row.seal_version_id,
        sealed=row.seal_storage_key is not None,
    )  # fmt: skip


async def _job(s: AsyncSession, job_id: uuid.UUID) -> Any:
    job = (
        await s.execute(
            text(
                "SELECT j.id, j.matter_id, j.status, j.sealed_at, m.closed_at AS matter_closed,"
                " c.closed_at AS client_closed FROM collection_jobs j JOIN matters m ON m.id = j.matter_id"
                " JOIN clients c ON c.id = m.client_id WHERE j.id = :j"
            ),
            {"j": job_id},
        )
    ).one_or_none()
    if job is None:
        raise not_found()
    return job


async def _report(
    s: AsyncSession, caller: Caller, permission: Permission, report_id: uuid.UUID
) -> Any:
    row = (
        await s.execute(text("SELECT * FROM reports WHERE id = :r"), {"r": report_id})
    ).one_or_none()
    if row is None:
        raise not_found()
    await authorize(s, caller, permission, Scope("matter", row.matter_id))
    return row


# ------------------------------------------------------------------ request
@router.post(
    "/jobs/{job_id}/reports",
    status_code=201,
    response_model=ReportOut,
    openapi_extra=perm(
        P.REPORT_CREATE,
        E.CLIENT_CLOSED,
        E.FORBIDDEN,
        E.JOB_NOT_SEALED,
        E.MATTER_CLOSED,
        E.NOT_FOUND,
        E.REAUTH_REQUIRED,
    ),
)
async def request_report(
    job_id: uuid.UUID, body: ReportIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> ReportOut:
    identity = new_report_identity(res.settings)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        job = await _job(s, job_id)
        await authorize(s, caller, P.REPORT_CREATE, Scope("matter", job.matter_id))
        signed_in = require_recent_sign_in(caller, res.settings)
        if job.sealed_at is None:
            raise ApiError(409, "job_not_sealed", f"job is {job.status}: only sealed jobs report")
        if job.matter_closed is not None:
            raise ApiError(409, "matter_closed", "the job's matter is closed")
        if job.client_closed is not None:
            raise ApiError(409, "client_closed", "the job's client is closed")
        made = await create_report(
            s, tenant_id=caller.tenant_id, job_id=job_id, matter_id=job.matter_id,
            requested_by=caller.actor, paper=body.paper, request_reason=body.reason,
            request_id=rid, identity=identity,
        )  # fmt: skip
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="report_requested",
            payload={"report_id": str(made.report_id), "job_id": str(job_id),
                     "matter_id": str(job.matter_id), "reason": body.reason, "paper": body.paper,
                     "identity": dict(identity), "authenticated_at": signed_in},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        row = (
            await s.execute(text("SELECT * FROM reports WHERE id = :r"), {"r": made.report_id})
        ).one()
    # on the queue of the report's recorded runtime, which only workers of that runtime poll
    await start_report_workflow(res.temporal, res.settings, caller.tenant_id, made.report_id,
                                identity)  # fmt: skip
    return _out(row)


# ------------------------------------------------------------------ read
@router.get(
    "/jobs/{job_id}/reports",
    response_model=Page[ReportOut],
    openapi_extra=perm(P.REPORT_READ, E.FORBIDDEN, E.NOT_FOUND, E.UNPROCESSABLE),
)
async def list_reports(
    job_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[ReportOut]:  # fmt: skip
    """Every report of the job, all generations, oldest first (ids are time-ordered UUIDv7)."""
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        job = await _job(s, job_id)
        await authorize(s, caller, P.REPORT_READ, Scope("matter", job.matter_id))
        rows = (
            await s.execute(
                text(
                    "SELECT * FROM reports WHERE job_id = :j"
                    " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
                ),
                {"j": job_id, "after": after, "n": limit + 1},
            )
        ).all()
    return page_of([_out(r) for r in rows], limit, lambda r: r.id)


@router.get(
    "/reports/{report_id}",
    response_model=ReportOut,
    openapi_extra=perm(P.REPORT_READ, E.FORBIDDEN, E.NOT_FOUND),
)
async def get_report(report_id: uuid.UUID, caller: CallerDep, res: ResourcesDep) -> ReportOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        return _out(await _report(s, caller, P.REPORT_READ, report_id))


@router.get(
    "/reports/{report_id}/files",
    response_model=list[ReportFileOut],
    openapi_extra=perm(P.REPORT_READ, E.FORBIDDEN, E.NOT_FOUND),
)
async def list_report_files(
    report_id: uuid.UUID, caller: CallerDep, res: ResourcesDep
) -> list[ReportFileOut]:
    """The report's stored files in order (a handful: bounded, so not paginated)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _report(s, caller, P.REPORT_READ, report_id)
        rows = (
            await s.execute(
                text(
                    "SELECT ord, name, media_type, sha256, size_bytes, rows, version_id"
                    " FROM report_files WHERE report_id = :r ORDER BY ord"
                ),
                {"r": report_id},
            )
        ).all()
    return [
        ReportFileOut(ord=r.ord, name=r.name, media_type=r.media_type, sha256=r.sha256,
                      size=r.size_bytes, rows=r.rows, version_id=r.version_id)
        for r in rows
    ]  # fmt: skip


# ------------------------------------------------------------------ download (always audited)
@router.get(
    "/reports/{report_id}/files/{name}/content",
    openapi_extra=perm(P.REPORT_READ, E.CONFLICT, E.FORBIDDEN, E.INTEGRITY, E.NOT_FOUND),
)
async def report_file_content(
    report_id: uuid.UUID, name: str, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> StreamingResponse:
    """One file of a completed report. The audit event is committed and anchored BEFORE any byte."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        row = await _report(s, caller, P.REPORT_READ, report_id)
        if row.status != "completed" or row.seal_storage_key is None:
            raise conflict(f"the report is {row.status}: only completed reports are downloaded")
        f = (
            await s.execute(
                text(
                    "SELECT f.name, f.media_type, f.evidence_object_id, f.sha256, f.size_bytes,"
                    " f.version_id, e.sha256 AS registry_sha, e.version_id AS registry_version"
                    " FROM report_files f JOIN evidence_objects e ON e.id = f.evidence_object_id"
                    " WHERE f.report_id = :r AND f.name = :n AND e.state = 'complete'"
                ),
                {"r": report_id, "n": name},
            )
        ).one_or_none()
        if f is None:
            raise not_found()
        if (f.registry_sha, f.registry_version) != (f.sha256, f.version_id):
            raise ApiError(409, "integrity", "the registry disagrees with the report's record")
        base = {"report_id": str(report_id), "job_id": str(row.job_id),
                "matter_id": str(row.matter_id), "name": f.name,
                "evidence_id": str(f.evidence_object_id), "sha256": f.sha256,
                "size": f.size_bytes}  # fmt: skip
        read = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="report_read",
            payload=base, request_id=rid,
        )  # fmt: skip
    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)
    writer = EvidenceWriter(res.sessions, res.s3, res.settings)

    async def checked() -> AsyncIterator[bytes]:
        """The pinned bytes, re-hashed as they pass: never more, nor other, than recorded."""
        digest, size = hashlib.sha256(), 0
        async for chunk in writer.open(
            tenant_id=caller.tenant_id, evidence_id=f.evidence_object_id
        ):
            digest.update(chunk)
            size += len(chunk)
            if size > f.size_bytes:
                raise PackageIntegrityError(f.name, "more bytes than recorded")
            yield chunk
        if (digest.hexdigest(), size) != (f.sha256, f.size_bytes):
            raise PackageIntegrityError(f.name, "stored bytes differ from the record")

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in checked():
                yield chunk
        except Exception as exc:
            await _aborted(
                res, caller, rid, row.job_id, "report_read_aborted", "report_read_mismatch",
                base, exc,
            )  # fmt: skip
            raise

    cut = Download("report_file_content", {"report_id": str(report_id), "name": f.name},
                   f.size_bytes, read.seq)  # fmt: skip
    return GuardedResponse(
        guarded(
            body(),
            res=res,
            caller=caller,
            request_id=rid,
            download=cut,
            permitted=still_permitted(res, caller, P.REPORT_READ, Scope("matter", row.matter_id)),
        ),
        media_type=f.media_type,
        headers={
            "x-evidence-sha256": f.sha256,
            "content-length": str(f.size_bytes),
            "content-disposition": f'attachment; filename="{f.name}"',
            "cache-control": "no-store",
        },
    )


@router.get(
    "/reports/{report_id}/package",
    openapi_extra=perm(P.REPORT_READ, E.FORBIDDEN, E.NOT_FOUND, E.REPORT_NOT_SEALED),
)
async def report_package(
    report_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> StreamingResponse:
    """The report package as one deterministic zip (``edisc-report-package/1``), for
    ``edisc-verify`` (with ``--job-package`` to recompute the chain-derived content)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        row = await _report(s, caller, P.REPORT_READ, report_id)
        if row.status != "completed" or row.seal_storage_key is None:
            raise ApiError(409, "report_not_sealed", f"the report is {row.status} and not sealed")
    plan = await plan_report_package(
        res.sessions, res.s3, res.settings, tenant_id=caller.tenant_id, report_id=report_id
    )
    base = {"report_id": str(report_id), "job_id": str(row.job_id),
            "matter_id": str(row.matter_id), "format": REPORT_PACKAGE_FORMAT,
            "manifest_sha256": plan.manifest_sha256, "size": plan.zip_size}  # fmt: skip
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        if plan.anchor_divergences:
            # served anyway (the package holds what the bucket holds), recorded and alerted
            await audit.record(
                s, tenant_id=caller.tenant_id, actor=caller.actor,
                event_type="report_package_anchor_divergence",
                payload={**base, "divergences": list(plan.anchor_divergences)}, request_id=rid,
            )  # fmt: skip
            await _alert(s, caller.tenant_id, row.job_id, "report_anchor_divergence",
                         f"report {report_id}: anchor listing differs from the records")  # fmt: skip
        read = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="report_package_read",
            payload=base, request_id=rid,
        )  # fmt: skip
    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in zip_stream(report_package_members(res.sessions, res.s3,
                                                                 res.settings, plan)):  # fmt: skip
                yield chunk
        except Exception as exc:
            await _aborted(res, caller, rid, row.job_id, "report_package_aborted",
                           "report_package_mismatch", base, exc)  # fmt: skip
            raise

    cut = Download("report_package", {"report_id": str(report_id)}, plan.zip_size, read.seq)
    return GuardedResponse(
        guarded(
            body(),
            res=res,
            caller=caller,
            request_id=rid,
            download=cut,
            permitted=still_permitted(res, caller, P.REPORT_READ, Scope("matter", row.matter_id)),
        ),
        media_type="application/zip",
        headers={
            "x-manifest-sha256": plan.manifest_sha256,
            "content-length": str(plan.zip_size),
            "content-disposition": f'attachment; filename="report-{report_id}.zip"',
            "cache-control": "no-store",
        },
    )


async def _alert(s: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID, kind: str,
                 message: str) -> None:  # fmt: skip
    await s.execute(
        text(
            "INSERT INTO alerts (id, tenant_id, kind, job_id, message) VALUES (:i, :t, :k, :j, :m)"
        ),
        {"i": new_id(), "t": tenant_id, "k": kind, "j": job_id, "m": message[:2000]},
    )


async def _aborted(
    res: Resources, caller: Caller, rid: str, job_id: uuid.UUID, event_type: str, alert: str,
    base: dict[str, Any], exc: Exception,
) -> None:  # fmt: skip
    """An aborted download: recorded (and anchored); an integrity failure also alerts."""
    integrity = isinstance(exc, (PackageIntegrityError, ZipSizeError))
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        event = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type=event_type,
            payload={**base, "integrity": integrity, "error": type(exc).__name__,
                     "entry": getattr(exc, "entry", None), "detail": str(exc)[:500]},
            request_id=rid,
        )  # fmt: skip
        if integrity:
            await _alert(s, caller.tenant_id, job_id, alert,
                         f"{base['report_id']}: {event_type}: {str(exc)[:500]}")  # fmt: skip
    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, event)
