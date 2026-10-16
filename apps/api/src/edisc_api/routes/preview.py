"""The HTML preview of a conversation-day (ADR 0018 §1.2, §2, §4.7, §10): ``GET
/v1/jobs/{id}/preview?conversation=&day=&zone=&page=``, under ``evidence.read`` (it is evidence
content: tenant admins, client admins, matter managers, reviewers).

Order (§10): authorize -> load the day (pinned versions, checked as the render loader does) ->
reconcile (every in-scope message of the day on exactly one page) -> render (bounded by the page cap)
-> SHA-256 -> commit `audit.preview_read` (job, conversation, day, zone, page, slice source hash,
preview identity, output SHA-256 and size) -> `audit.anchor_now` -> the first byte. A failure after
authorization records `audit.preview_refused` with its code (an integrity failure also raises a
`preview_integrity` alert) and answers 409 (`job_not_sealed`), 404 (`not_in_scope`,
`page_not_found`), 422 (`unknown_time_zone`) or 500 (`preview_integrity`). Nothing is stored.

Headers (D10): the CSP of §10 (also, minus `frame-ancestors` and `sandbox`, which a `<meta>` policy
ignores, in the document), `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`,
`Referrer-Policy: no-referrer`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import Caller
from edisc_api.authz import P, Scope, authorize, perm
from edisc_api.downloads import Download, GuardedResponse, guarded, one_chunk, still_permitted
from edisc_api.error_codes import E
from edisc_api.errors import ApiError, not_found
from edisc_core.ids import new_id
from edisc_db.session import tenant_tx
from edisc_renderers.preview import CSP_HEADER
from edisc_worker.preview import PreviewIntegrityError, PreviewRefusedError, build_preview

router = APIRouter(prefix="/v1")

REFUSED_STATUS = {
    "job_not_sealed": 409,
    "not_in_scope": 404,
    "page_not_found": 404,
    "unknown_time_zone": 422,
}
REFUSED_DETAIL = {
    "job_not_sealed": "only sealed jobs are previewed",
    "not_in_scope": "the day is outside every scope of the job for this conversation",
    "page_not_found": "the day has no such page",
    "unknown_time_zone": "unknown time zone (IANA names of the pinned tzdata)",
}
HEADERS = {
    "content-security-policy": CSP_HEADER,
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
}


@router.get(
    "/jobs/{job_id}/preview",
    openapi_extra=perm(
        P.EVIDENCE_READ,
        E.FORBIDDEN,
        E.JOB_NOT_SEALED,
        E.NOT_FOUND,
        E.NOT_IN_SCOPE,
        E.PAGE_NOT_FOUND,
        E.PREVIEW_INTEGRITY,
        E.UNKNOWN_TIME_ZONE,
    ),
)
async def preview(
    job_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
    conversation: Annotated[str, Query(min_length=1, max_length=255)],
    day: Annotated[date, Query()],
    zone: Annotated[str, Query(min_length=1, max_length=64)] = "UTC",
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
) -> StreamingResponse:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        job = (
            await s.execute(
                text("SELECT matter_id FROM collection_jobs WHERE id = :j"), {"j": job_id}
            )
        ).one_or_none()
        if job is None:
            raise not_found()
        await authorize(s, caller, P.EVIDENCE_READ, Scope("matter", job.matter_id))
    request = {"job_id": str(job_id), "matter_id": str(job.matter_id),
               "conversation_id": conversation, "day": day.isoformat(), "zone": zone,
               "page": page}  # fmt: skip
    try:
        result = await build_preview(
            res.sessions, res.s3, res.settings, tenant_id=caller.tenant_id, job_id=job_id,
            conversation_id=conversation, day=day, time_zone=zone, page=page,
        )  # fmt: skip
    except PreviewRefusedError as exc:
        await _refused(res, caller, rid, job_id, request, exc.code, exc.detail, integrity=False)
        # the audit keeps the detail; the answer never quotes the request (D11)
        raise ApiError(REFUSED_STATUS[exc.code], exc.code, REFUSED_DETAIL[exc.code]) from exc
    except PreviewIntegrityError as exc:
        await _refused(res, caller, rid, job_id, request, "preview_integrity", str(exc),
                       integrity=True)  # fmt: skip
        raise ApiError(500, "preview_integrity", "the day's evidence did not verify") from exc
    out = result.page
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        read = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="preview_read",
            payload={**request, "pages": out.pages, "events": out.events,
                     "context_events": out.context_events, "day_messages": out.day_messages,
                     "slice_source_hash": out.source_hash, "identity": out.identity,
                     "sha256": out.sha256, "size": len(out.html)},
            request_id=rid,
        )  # fmt: skip
    # committed AND anchored (forced, checked to cover the read) before any byte (§10)
    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)
    cut = Download("preview", {"job_id": str(job_id), "conversation_id": conversation,
                                   "day": day.isoformat(), "page": page},
                   len(out.html), read.seq)  # fmt: skip
    return GuardedResponse(
        guarded(
            one_chunk(out.html),
            res=res,
            caller=caller,
            request_id=rid,
            download=cut,
            permitted=still_permitted(res, caller, P.EVIDENCE_READ, Scope("matter", job.matter_id)),
        ),
        media_type="text/html; charset=utf-8",
        headers={**HEADERS, "x-preview-sha256": out.sha256, "content-length": str(len(out.html))},
    )


async def _refused(
    res: Resources, caller: Caller, rid: str, job_id: uuid.UUID,
    request: dict[str, object], code: str, detail: str, *, integrity: bool,
) -> None:  # fmt: skip
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        event = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="preview_refused",
            payload={**request, "code": code, "detail": detail[:2000]}, request_id=rid,
        )  # fmt: skip
        if integrity:
            await s.execute(
                text(
                    "INSERT INTO alerts (id, tenant_id, kind, job_id, message)"
                    " VALUES (:i, :t, 'preview_integrity', :j, :m)"
                ),
                {"i": new_id(), "t": caller.tenant_id, "j": job_id,
                 "m": f"preview refused: {detail}"[:2000]},
            )  # fmt: skip
    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, event)
