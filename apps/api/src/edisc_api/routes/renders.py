"""RSMF renders (ADR 0015 §7 and §14): create, status, files, custody verification, audited downloads.

- **Create** ``POST /v1/jobs/{id}/renders`` (``export.create``: matter managers and tenant admins; the
  recent-sign-in check of ADR 0016 §4 plugs in at ``require_recent_sign_in``). Refused with 409 and
  nothing created when the job is not sealed or its matter or client is closed. A chain that fails
  verification is found by the workflow, which records a ``refused`` render. One live render per
  (job, options, renderer, Unicode and tzdata versions): an identical request returns it (200)
  instead of creating another; an ``Idempotency-Key`` replays its original request.
- **Read** status, files and custody verification: ``custody.read`` (auditors see these, not bytes).
- **Download** a file: ``export.read`` (matter managers, tenant admins). The read is audited and the
  audit anchored BEFORE any byte is returned; bytes come from the pinned version and are re-hashed as
  they stream (a mismatch aborts the response and raises an alert).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, ResourcesDep
from edisc_api.auth import Caller, require_recent_sign_in
from edisc_api.authz import P, Permission, Scope, authorize, perm
from edisc_api.errors import ApiError, conflict, not_found, unprocessable
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, decode_text, encode_text, page_of
from edisc_api.routes.jobs import IdempotencyKey, _claim_key, _request_hash
from edisc_core.ids import new_id
from edisc_custody.log import verify_chain
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_renderers.rsmf import RenderInputError, RenderOptions
from edisc_worker.renders import create_render, options_hash, start_render_workflow

router = APIRouter(prefix="/v1")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenderIn(Strict):
    time_zone: str = Field(default="UTC", min_length=1, max_length=64)
    include_context: bool = True


class RenderOut(Strict):
    id: uuid.UUID
    job_id: uuid.UUID
    matter_id: uuid.UUID
    status: str
    reason: str | None
    detail: str | None
    options: dict[str, Any]
    renderer_version: str
    unicode_version: str
    tzdata_version: str
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    job_head_seq: int | None  # the sealed job this render references (render_started)
    job_head_hash: str | None
    job_seal_key: str | None
    job_seal_version: str | None
    files_done: int
    file_count: int | None
    summary: dict[str, Any] | None  # the reconciliation summary
    head_seq: int | None  # the render's own custody stream, once sealed
    head_hash: str | None
    seal_storage_key: str | None
    seal_version_id: str | None
    sealed: bool
    # sealing is retried without limit; this flags a render whose seal is overdue (ADR 0015 §15)
    sealing_stuck: bool
    sealing_stuck_since: datetime | None
    seal_failures: int
    last_seal_error: str | None


class RenderFileOut(Strict):
    ord: int
    name: str
    evidence_id: uuid.UUID
    version_id: str
    sha256: str
    size: int
    conversation_id: str
    day: str
    time_zone: str
    part: int
    parts: int
    source_hash: str
    event_count: int
    context_event_count: int
    attachment_count: int
    unavailable_count: int


class RenderVerifyOut(Strict):
    ok: bool
    events: int
    batches_checked: int
    files_checked: int
    anchors_checked: int
    errors: list[str]


# ------------------------------------------------------------------ helpers
async def _job_for_render(s: AsyncSession, job_id: uuid.UUID) -> Any:
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


async def _authorize_render(
    s: AsyncSession, caller: Caller, permission: Permission, render_id: uuid.UUID
) -> Any:
    row = (
        await s.execute(text("SELECT * FROM renders WHERE id = :r"), {"r": render_id})
    ).one_or_none()
    if row is None:
        raise not_found()
    await authorize(s, caller, permission, Scope("matter", row.matter_id))
    return row


def _out(row: Any) -> RenderOut:
    return RenderOut(
        id=row.id,
        job_id=row.job_id,
        matter_id=row.matter_id,
        status=row.status,
        reason=row.reason,
        detail=row.detail,
        options=dict(row.options),
        renderer_version=row.renderer_version,
        unicode_version=row.unicode_version,
        tzdata_version=row.tzdata_version,
        requested_by=row.requested_by,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        job_head_seq=row.job_head_seq,
        job_head_hash=row.job_head_hash,
        job_seal_key=row.job_seal_key,
        job_seal_version=row.job_seal_version,
        files_done=row.files_done,
        file_count=row.file_count,
        summary=dict(row.summary) if row.summary is not None else None,
        head_seq=row.head_seq,
        head_hash=row.head_hash,
        seal_storage_key=row.seal_storage_key,
        seal_version_id=row.seal_version_id,
        sealed=row.seal_storage_key is not None,
        sealing_stuck=row.sealing_stuck_at is not None and row.seal_storage_key is None,
        sealing_stuck_since=row.sealing_stuck_at,
        seal_failures=row.seal_failures,
        last_seal_error=row.last_seal_error,
    )


# ------------------------------------------------------------------ create
@router.post(
    "/jobs/{job_id}/renders",
    status_code=201,
    response_model=RenderOut,
    openapi_extra=perm(P.EXPORT_CREATE),
)
async def create_render_route(
    job_id: uuid.UUID,
    body: RenderIn,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> RenderOut:
    try:
        options = RenderOptions(include_context=body.include_context, time_zone=body.time_zone)
    except RenderInputError as exc:
        raise unprocessable(str(exc)) from exc
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        job = await _job_for_render(s, job_id)
        await authorize(s, caller, P.EXPORT_CREATE, Scope("matter", job.matter_id))
        require_recent_sign_in(caller)
        if job.sealed_at is None:
            raise ApiError(409, "job_not_sealed", f"job is {job.status}: only sealed jobs render")
        if job.matter_closed is not None:
            raise ApiError(409, "matter_closed", "the job's matter is closed")
        if job.client_closed is not None:
            raise ApiError(409, "client_closed", "the job's client is closed")
        replay = None
        if idempotency_key:
            replay = await _claim_key(
                s, caller, idempotency_key, _request_hash(job.matter_id, body, f"render:{job_id}"),
                target="render_id",
            )  # fmt: skip
        if replay is not None:
            render_id = replay
        else:
            made = await create_render(
                s, tenant_id=caller.tenant_id, job_id=job_id, matter_id=job.matter_id,
                options=options, requested_by=caller.actor, request_id=rid,
                idempotency_key=idempotency_key,
            )  # fmt: skip
            render_id = made.render_id
            if idempotency_key:
                await s.execute(
                    text("UPDATE api_idempotency SET render_id = :r WHERE key = :k"),
                    {"r": render_id, "k": idempotency_key},
                )
            await audit.record(
                s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="render_requested",
                payload={"render_id": str(render_id), "job_id": str(job_id),
                         "matter_id": str(job.matter_id), "options": options.as_payload(),
                         "options_hash": options_hash(options), "created": made.created,
                         **({"idempotency_key": idempotency_key} if idempotency_key else {})},
                request_id=rid,
            )  # fmt: skip
            if not made.created:
                response.status_code = 200
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        row = (await s.execute(text("SELECT * FROM renders WHERE id = :r"), {"r": render_id})).one()
    # on the queue of the render's recorded versions, which only workers of those versions poll
    await start_render_workflow(
        res.temporal, res.settings, caller.tenant_id, render_id,
        {k: getattr(row, k) for k in ("renderer_version", "unicode_version", "tzdata_version")},
    )  # fmt: skip
    return _out(row)


# ------------------------------------------------------------------ read
@router.get(
    "/jobs/{job_id}/renders", response_model=Page[RenderOut], openapi_extra=perm(P.CUSTODY_READ)
)
async def list_renders(
    job_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[RenderOut]:  # fmt: skip
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        job = await _job_for_render(s, job_id)
        await authorize(s, caller, P.CUSTODY_READ, Scope("matter", job.matter_id))
        rows = (
            await s.execute(
                text(
                    "SELECT * FROM renders WHERE job_id = :j"
                    " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
                ),
                {"j": job_id, "after": after, "n": limit + 1},
            )
        ).all()
    return page_of([_out(r) for r in rows], limit, lambda r: r.id)


@router.get("/renders/{render_id}", response_model=RenderOut, openapi_extra=perm(P.CUSTODY_READ))
async def get_render(render_id: uuid.UUID, caller: CallerDep, res: ResourcesDep) -> RenderOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        return _out(await _authorize_render(s, caller, P.CUSTODY_READ, render_id))


@router.get(
    "/renders/{render_id}/files",
    response_model=Page[RenderFileOut],
    openapi_extra=perm(P.CUSTODY_READ),
)
async def list_render_files(
    render_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[RenderFileOut]:  # fmt: skip
    raw = decode_text(cursor)
    try:
        after = int(raw) if raw is not None else -1
    except ValueError as exc:
        raise unprocessable("invalid cursor") from exc
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_render(s, caller, P.CUSTODY_READ, render_id)
        rows = (
            await s.execute(
                text(
                    "SELECT ord, name, evidence_object_id, version_id, sha256, size_bytes, record"
                    " FROM render_files WHERE render_id = :r AND ord > :a ORDER BY ord LIMIT :n"
                ),
                {"r": render_id, "a": after, "n": limit + 1},
            )
        ).all()
    files = [
        RenderFileOut(
            ord=r.ord,
            name=r.name,
            evidence_id=r.evidence_object_id,
            version_id=r.version_id,
            sha256=r.sha256,
            size=r.size_bytes,
            conversation_id=r.record["conversation_id"],
            day=r.record["day"],
            time_zone=r.record["time_zone"],
            part=r.record["part"],
            parts=r.record["parts"],
            source_hash=r.record["source_hash"],
            event_count=r.record["event_count"],
            context_event_count=r.record["context_event_count"],
            attachment_count=r.record["attachment_count"],
            unavailable_count=r.record["unavailable_count"],
        )
        for r in rows
    ]
    more = len(files) > limit
    files = files[:limit]
    return Page[RenderFileOut](
        items=files, next_cursor=encode_text(str(files[-1].ord)) if more else None
    )


@router.get(
    "/renders/{render_id}/custody/verify",
    response_model=RenderVerifyOut,
    openapi_extra=perm(P.CUSTODY_READ),
)
async def verify_render(
    render_id: uuid.UUID, caller: CallerDep, res: ResourcesDep
) -> RenderVerifyOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_render(s, caller, P.CUSTODY_READ, render_id)
    report = await verify_chain(
        res.sessions, res.s3, res.settings, tenant_id=caller.tenant_id, stream_id=render_id
    )
    return RenderVerifyOut(
        ok=report.ok,
        events=report.events,
        batches_checked=report.batches_checked,
        files_checked=report.files_checked,
        anchors_checked=report.anchors_checked,
        errors=report.errors[:100],
    )


# ------------------------------------------------------------------ download (always audited)
@router.get("/renders/{render_id}/files/{file_ord}/content", openapi_extra=perm(P.EXPORT_READ))
async def render_file_content(
    render_id: uuid.UUID,
    file_ord: Annotated[int, Path(ge=0)],
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
) -> StreamingResponse:
    """One output file of a completed render. The audit event is committed (and anchored) BEFORE any
    byte is returned: a download that is not recorded never happens."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        row = await _authorize_render(s, caller, P.EXPORT_READ, render_id)
        if row.status != "completed":
            raise conflict(f"the render is {row.status}: only completed renders are downloaded")
        f = (
            await s.execute(
                text(
                    "SELECT rf.name, rf.evidence_object_id, rf.sha256, rf.size_bytes, e.sha256 AS registry_sha"
                    " FROM render_files rf JOIN evidence_objects e ON e.id = rf.evidence_object_id"
                    " WHERE rf.render_id = :r AND rf.ord = :o AND e.state = 'complete'"
                ),
                {"r": render_id, "o": file_ord},
            )
        ).one_or_none()
        if f is None:
            raise not_found()
        if f.registry_sha != f.sha256:
            raise ApiError(409, "integrity", "the registry disagrees with the render's record")
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="render_file_read",
            payload={"render_id": str(render_id), "job_id": str(row.job_id),
                     "matter_id": str(row.matter_id), "ord": file_ord, "name": f.name,
                     "evidence_id": str(f.evidence_object_id), "sha256": f.sha256,
                     "size": f.size_bytes, "purpose": "rsmf"},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    writer = EvidenceWriter(res.sessions, res.s3, res.settings)

    async def body() -> AsyncIterator[bytes]:
        digest, size = hashlib.sha256(), 0
        async for chunk in writer.open(
            tenant_id=caller.tenant_id, evidence_id=f.evidence_object_id
        ):
            digest.update(chunk)
            size += len(chunk)
            yield chunk
        if (digest.hexdigest(), size) != (f.sha256, f.size_bytes):
            async with tenant_tx(res.sessions, caller.tenant_id) as s:
                await s.execute(
                    text(
                        "INSERT INTO alerts (id, tenant_id, kind, job_id, message)"
                        " VALUES (:i, :t, 'production_mismatch', :j, :m)"
                    ),
                    {"i": new_id(), "t": caller.tenant_id, "j": row.job_id,
                     "m": f"render {render_id} file {file_ord}: stored bytes differ from the record"},
                )  # fmt: skip
            raise RuntimeError(f"render {render_id} file {file_ord}: bytes differ from the record")

    return StreamingResponse(
        body(),
        media_type="message/rfc822",
        headers={
            "x-evidence-sha256": f.sha256,
            "content-disposition": f'attachment; filename="{f.name}"',
            "cache-control": "no-store",
        },
    )
