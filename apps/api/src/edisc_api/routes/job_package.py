"""The job custody package download (ADR 0008, ADR 0018 §27): ``GET /v1/jobs/{id}/package``.

Same rules as the render package (ADR 0015 §19): ``export.read`` (matter managers, tenant admins),
sealed jobs only (409 otherwise); the package (``edisc-custody-package/3``) is planned from the
records first (``plan_job_package``: no evidence body read); ``audit.job_package_read`` with the
manifest's SHA-256 is committed AND anchored before any byte; then one streaming pass checks every
entry against the plan, every object read by its pinned version and re-hashed as it passes. A
mismatch aborts the stream, records ``audit.job_package_aborted`` and raises an alert. The zip is
deterministic (fixed metadata, the seal time instead of an export time) with an exact
``Content-Length``: two downloads of one job in one mode are byte-identical.

``objects=embed`` (default) includes every collected page and file (``objects/<sha256>``);
``omit`` only the records. ``archives=reference`` (default) lists export zips by SHA-256 and size
(the expert supplies them with ``edisc-verify --archive``); ``embed`` includes them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import Caller
from edisc_api.authz import P, Scope, authorize, perm
from edisc_api.downloads import Download, GuardedResponse, guarded, still_permitted
from edisc_api.error_codes import E
from edisc_api.errors import ApiError, not_found
from edisc_core.ids import new_id
from edisc_custody.job_export import (
    JobPackagePlan,
    PackageIntegrityError,
    job_package_members,
    plan_job_package,
)
from edisc_custody.zipwriter import ZipSizeError, zip_stream
from edisc_db.session import tenant_tx
from edisc_verify.package import PACKAGE_FORMAT

router = APIRouter(prefix="/v1")


@router.get(
    "/jobs/{job_id}/package",
    openapi_extra=perm(P.EXPORT_READ, E.FORBIDDEN, E.JOB_NOT_SEALED, E.NOT_FOUND),
)
async def job_package(
    job_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
    objects: Annotated[Literal["embed", "omit"], Query()] = "embed",
    archives: Annotated[Literal["embed", "reference"], Query()] = "reference",
) -> StreamingResponse:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        job = (
            await s.execute(
                text("SELECT id, matter_id, status, sealed_at FROM collection_jobs WHERE id = :j"),
                {"j": job_id},
            )
        ).one_or_none()
        if job is None:
            raise not_found()
        await authorize(s, caller, P.EXPORT_READ, Scope("matter", job.matter_id))
        if job.sealed_at is None:
            raise ApiError(409, "job_not_sealed", f"the job is {job.status} and not sealed yet")
    plan = await plan_job_package(
        res.sessions, res.s3, res.settings, tenant_id=caller.tenant_id, job_id=job_id,
        objects=objects, archives=archives,
    )  # fmt: skip
    base = {"job_id": str(job_id), "matter_id": str(job.matter_id), "objects": objects,
            "archives": archives, "format": PACKAGE_FORMAT,
            "manifest_sha256": plan.manifest_sha256, "object_count": len(plan.objects),
            "size": plan.zip_size}  # fmt: skip
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        read = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="job_package_read",
            payload=base, request_id=rid,
        )  # fmt: skip
    # committed AND anchored (forced, checked to cover the read) before any byte
    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, read)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in zip_stream(job_package_members(res.sessions, res.s3, res.settings,
                                                              plan)):  # fmt: skip
                yield chunk
        except Exception as exc:
            await _aborted(res, caller, rid, plan, base, exc)
            raise  # the response is aborted, never completed

    cut = Download("job_package", {"job_id": str(job_id), "objects": objects,
                                   "archives": archives}, plan.zip_size, read.seq)  # fmt: skip
    return GuardedResponse(
        guarded(
            body(),
            res=res,
            caller=caller,
            request_id=rid,
            download=cut,
            permitted=still_permitted(res, caller, P.EXPORT_READ, Scope("matter", job.matter_id)),
        ),
        media_type="application/zip",
        headers={
            "x-manifest-sha256": plan.manifest_sha256,
            "content-length": str(plan.zip_size),
            "content-disposition": f'attachment; filename="job-{job_id}-{objects}-{archives}.zip"',
            "cache-control": "no-store",
        },
    )


async def _aborted(
    res: Resources, caller: Caller, rid: str, plan: JobPackagePlan, base: dict[str, Any],
    exc: Exception,
) -> None:  # fmt: skip
    integrity = isinstance(exc, (PackageIntegrityError, ZipSizeError))
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        event = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="job_package_aborted",
            payload={**base, "integrity": integrity, "error": type(exc).__name__,
                     "entry": getattr(exc, "entry", None), "detail": str(exc)[:500]},
            request_id=rid,
        )  # fmt: skip
        if integrity:
            await s.execute(
                text(
                    "INSERT INTO alerts (id, tenant_id, kind, job_id, message)"
                    " VALUES (:i, :t, 'job_package_mismatch', :j, :m)"
                ),
                {"i": new_id(), "t": caller.tenant_id, "j": plan.job_id,
                 "m": f"job {plan.job_id} package aborted: {str(exc)[:500]}"},
            )  # fmt: skip
    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, event)
