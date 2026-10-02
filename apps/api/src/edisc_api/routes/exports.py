"""Slack export uploads (ADR 0014 section 1): a resumable, client-owned upload session.

- ``POST /v1/clients/{c}/exports`` opens a session (``connection.manage``). The body declares the size
  and, optionally, the client's SHA-256, the plan, and per-upload limit overrides. Overrides need a
  tenant admin and each one is audited with the default and the override value (R1).
- ``PUT /v1/exports/{id}/parts/{n}`` stores one part in the staging multipart upload. The part must carry
  ``Content-Digest: sha-256=:<base64>:`` (RFC 9530); we hash the body ourselves, refuse a mismatch, and
  hand the same digest to the store, which verifies it again. A part may be re-sent until completion.
- ``POST /v1/exports/{id}/complete`` checks the parts (contiguous, sizes, sum = declared size, and the
  store still holds exactly the recorded parts), then starts the hash-and-lock workflow. It waits briefly
  for the lock step: a declared SHA-256 that differs from ours is a 422 with both hashes (R7). Later calls
  and ``GET`` report the same outcome.

Nothing touches local disk; the API holds at most one part per request in memory.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import Caller
from edisc_api.authz import TENANT, P, Permission, Scope, authorize, perm, permissions, roles_at
from edisc_api.errors import ApiError, conflict, forbidden, not_found, unprocessable
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, page_of
from edisc_api.routes.hierarchy import ensure_client_open
from edisc_core.ids import new_id
from edisc_db.session import tenant_tx
from edisc_worker.contracts import EXPORTS_QUEUE, ExportRef, export_workflow_id
from edisc_worker.exports import default_limits

router = APIRouter(prefix="/v1")

PLANS = ("free", "pro", "business_plus", "enterprise_grid")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LimitOverrides(Strict):
    max_archive_bytes: int | None = Field(default=None, ge=1)
    max_entries: int | None = Field(default=None, ge=1)
    max_entry_bytes: int | None = Field(default=None, ge=1)
    max_total_bytes: int | None = Field(default=None, ge=1)
    max_total_ratio: int | None = Field(default=None, ge=1)
    max_entry_ratio: int | None = Field(default=None, ge=1)
    ratio_floor_bytes: int | None = Field(default=None, ge=0)
    max_name_bytes: int | None = Field(default=None, ge=16)


class ExportIn(Strict):
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    plan: str | None = Field(default=None, pattern="^(" + "|".join(PLANS) + ")$")
    limits: LimitOverrides | None = None


class UploadInfo(Strict):
    part_min_bytes: int
    part_max_bytes: int
    parts_received: int
    bytes_received: int
    expires_at: datetime


class ExportOut(Strict):
    id: uuid.UUID
    client_id: uuid.UUID
    status: str
    reject_reason: str | None
    reject_detail: dict[str, Any] | None
    declared_size: int
    declared_sha256: str | None
    declared_plan: str | None
    limits: dict[str, int]
    sha256: str | None
    size_bytes: int | None
    evidence_object_id: uuid.UUID | None
    version_id: str | None
    entry_count: int | None
    detected_tier: str | None
    tier_confirmed: bool | None
    findings: dict[str, Any]
    connection_id: uuid.UUID | None
    created_by: str
    created_at: datetime
    locked_at: datetime | None
    validated_at: datetime | None
    upload: UploadInfo | None


class PartOut(Strict):
    part_number: int
    size_bytes: int
    sha256: str


SELECT_ONE = (
    "SELECT id, client_id, status, reject_reason, reject_detail, declared_size, declared_sha256,"
    " declared_plan, limits, sha256, size_bytes, evidence_object_id, version_id, entry_count,"
    " detected_tier, tier_confirmed, findings, connection_id, created_by, created_at, locked_at,"
    " validated_at FROM slack_exports WHERE id = :i"
)


# ------------------------------------------------------------------ helpers
async def _export_client(s: AsyncSession, export_id: uuid.UUID) -> uuid.UUID:
    client: uuid.UUID | None = (
        await s.execute(text("SELECT client_id FROM slack_exports WHERE id = :i"), {"i": export_id})
    ).scalar_one_or_none()
    if client is None:
        raise not_found()
    return client


async def _authorize_export(
    s: AsyncSession, caller: Caller, permission: Permission, export_id: uuid.UUID
) -> None:
    await authorize(s, caller, permission, Scope("client", await _export_client(s, export_id)))


async def _out(s: AsyncSession, res: Resources, export_id: uuid.UUID) -> ExportOut:
    row = (await s.execute(text(SELECT_ONE), {"i": export_id})).one()
    upload = None
    if row.status == "uploading":
        n, total = (
            await s.execute(
                text(
                    "SELECT count(*), coalesce(sum(size_bytes), 0) FROM export_upload_parts"
                    " WHERE export_id = :i"
                ),
                {"i": export_id},
            )
        ).one()
        upload = UploadInfo(
            part_min_bytes=res.settings.export_upload_part_min_bytes,
            part_max_bytes=res.settings.export_upload_part_max_bytes,
            parts_received=n,
            bytes_received=int(total),
            expires_at=row.created_at + timedelta(days=res.settings.export_upload_ttl_days),
        )
    return ExportOut(**row._mapping, upload=upload)


def _digest(header: str | None) -> bytes:
    """The SHA-256 from ``Content-Digest`` (RFC 9530: ``sha-256=:<base64>:``, other algorithms ignored)."""
    if header:
        for item in header.split(","):
            name, _, value = item.strip().partition("=")
            if name.strip().lower() == "sha-256" and value.startswith(":") and value.endswith(":"):
                try:
                    raw = base64.b64decode(value[1:-1], validate=True)
                except binascii.Error:
                    break
                if len(raw) == 32:
                    return raw
    raise ApiError(
        400, "missing_digest", "each part needs a Content-Digest: sha-256=:<base64>: header"
    )


async def _read_part(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "part_too_large", f"parts are at most {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise ApiError(413, "part_too_large", f"parts are at most {limit} bytes")
    if not body:
        raise unprocessable("empty part")
    return bytes(body)


def _staging_key(tenant_id: uuid.UUID, export_id: uuid.UUID) -> str:
    """Under ``exports/``: the staging lifecycle keeps these for the upload TTL, not one day."""
    return f"exports/{tenant_id}/{export_id}"


# ------------------------------------------------------------------ routes
@router.post(
    "/clients/{client_id}/exports", status_code=201, response_model=ExportOut,
    openapi_extra=perm(P.CONNECTION_MANAGE),
)  # fmt: skip
async def create_export(
    client_id: uuid.UUID, body: ExportIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> ExportOut:
    defaults = default_limits(res.settings)
    overrides = (
        {k: v for k, v in body.limits.model_dump().items() if v is not None} if body.limits else {}
    )
    limits = {**defaults, **overrides}
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CONNECTION_MANAGE, Scope("client", client_id))
        await ensure_client_open(s, client_id)
        if overrides and P.TENANT_ADMIN not in permissions(await roles_at(s, caller, [TENANT])):
            raise forbidden("overriding archive limits needs a tenant admin")
    if body.size_bytes > limits["max_archive_bytes"]:
        raise ApiError(
            413, "export_too_large", f"exports are at most {limits['max_archive_bytes']} bytes"
        )
    export_id = new_id()
    key = _staging_key(caller.tenant_id, export_id)
    created = await res.s3.create_multipart_upload(
        Bucket=res.settings.s3_staging_bucket, Key=key, ChecksumAlgorithm="SHA256"
    )
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO slack_exports (id, tenant_id, client_id, declared_size, declared_sha256,"
                " declared_plan, limits, staging_key, upload_id, created_by) VALUES (:i, :t, :c, :n,"
                " :h, :p, CAST(:l AS jsonb), :k, :u, :by)"
            ),
            {"i": export_id, "t": caller.tenant_id, "c": client_id, "n": body.size_bytes,
             "h": body.sha256, "p": body.plan, "l": json.dumps(limits), "k": key,
             "u": created["UploadId"], "by": caller.actor},
        )  # fmt: skip
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="export_upload_started",
            payload={"export_id": str(export_id), "client_id": str(client_id),
                     "declared_size": body.size_bytes, "declared_sha256": body.sha256,
                     "declared_plan": body.plan},
            request_id=rid,
        )  # fmt: skip
        if overrides:
            await audit.record(
                s, tenant_id=caller.tenant_id, actor=caller.actor,
                event_type="export_limits_overridden",
                payload={"export_id": str(export_id),
                         "overrides": {k: {"default": defaults[k], "override": v}
                                       for k, v in sorted(overrides.items())}},
                request_id=rid,
            )  # fmt: skip
        out = await _out(s, res, export_id)
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return out


@router.put(
    "/exports/{export_id}/parts/{part_number}", response_model=PartOut,
    openapi_extra=perm(P.CONNECTION_MANAGE),
)  # fmt: skip
async def upload_part(
    export_id: uuid.UUID, part_number: int, request: Request, caller: CallerDep, res: ResourcesDep
) -> PartOut:
    if not 1 <= part_number <= 10000:
        raise unprocessable("part numbers run from 1 to 10000")
    digest = _digest(request.headers.get("content-digest"))
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_export(s, caller, P.CONNECTION_MANAGE, export_id)
        row = (
            await s.execute(
                text(
                    "SELECT status, declared_size, staging_key, upload_id FROM slack_exports"
                    " WHERE id = :i"
                ),
                {"i": export_id},
            )
        ).one()
        others: int = (
            await s.execute(
                text(
                    "SELECT coalesce(sum(size_bytes), 0) FROM export_upload_parts"
                    " WHERE export_id = :i AND part_number <> :n"
                ),
                {"i": export_id, "n": part_number},
            )
        ).scalar_one()
    if row.status != "uploading":
        raise conflict(f"export is {row.status}; parts are accepted only while uploading")
    data = await _read_part(request, res.settings.export_upload_part_max_bytes)
    ours = hashlib.sha256(data).digest()
    if ours != digest:
        raise ApiError(
            422, "digest_mismatch", "the part does not match its Content-Digest",
            {"received_sha256": ours.hex(), "declared_sha256": digest.hex()},
        )  # fmt: skip
    if int(others) + len(data) > row.declared_size:
        raise unprocessable("the parts would exceed the declared export size")
    try:
        stored = await res.s3.upload_part(
            Bucket=res.settings.s3_staging_bucket,
            Key=row.staging_key,
            UploadId=row.upload_id,
            PartNumber=part_number,
            Body=data,
            ChecksumSHA256=base64.b64encode(ours).decode(),
        )
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) == "NoSuchUpload":
            raise ApiError(
                410, "upload_expired", "this upload expired; start a new export"
            ) from exc
        raise
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        status: str = (
            await s.execute(
                text("SELECT status FROM slack_exports WHERE id = :i FOR SHARE"), {"i": export_id}
            )
        ).scalar_one()
        if status != "uploading":  # completed while this part was in flight: it is not part of it
            raise conflict(f"export is {status}; parts are accepted only while uploading")
        await s.execute(
            text(
                "INSERT INTO export_upload_parts (tenant_id, export_id, part_number, size_bytes,"
                " sha256, etag) VALUES (:t, :i, :n, :size, :h, :etag)"
                " ON CONFLICT (export_id, part_number) DO UPDATE SET size_bytes = EXCLUDED.size_bytes,"
                " sha256 = EXCLUDED.sha256, etag = EXCLUDED.etag, updated_at = now()"
            ),
            {"t": caller.tenant_id, "i": export_id, "n": part_number, "size": len(data),
             "h": ours.hex(), "etag": stored["ETag"]},
        )  # fmt: skip
    return PartOut(part_number=part_number, size_bytes=len(data), sha256=ours.hex())


@router.post(
    "/exports/{export_id}/complete", status_code=202, response_model=ExportOut,
    openapi_extra=perm(P.CONNECTION_MANAGE),
)  # fmt: skip
async def complete_export(
    export_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> ExportOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_export(s, caller, P.CONNECTION_MANAGE, export_id)
        row = (
            await s.execute(
                text(
                    "SELECT status, declared_size, staging_key, upload_id FROM slack_exports WHERE id = :i"
                ),
                {"i": export_id},
            )
        ).one()
        parts = await _parts(s, export_id)
    if row.status == "uploading":
        _check_parts(parts, row.declared_size, res.settings.export_upload_part_min_bytes)
        await _check_stored_parts(res, row.staging_key, row.upload_id, parts)
        async with tenant_tx(res.sessions, caller.tenant_id) as s:
            status: str = (
                await s.execute(
                    text("SELECT status FROM slack_exports WHERE id = :i FOR UPDATE"),
                    {"i": export_id},
                )
            ).scalar_one()
            if status == "uploading":
                if await _parts(s, export_id) != parts:
                    raise conflict("parts changed while completing; call complete again")
                await s.execute(
                    text(
                        "UPDATE slack_exports SET status = 'locking', updated_at = now() WHERE id = :i"
                    ),
                    {"i": export_id},
                )
                await audit.record(
                    s, tenant_id=caller.tenant_id, actor=caller.actor,
                    event_type="export_upload_completed",
                    payload={"export_id": str(export_id), "parts": len(parts),
                             "size_bytes": sum(p[1] for p in parts)},
                    request_id=rid,
                )  # fmt: skip
        await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    await _start(res, caller.tenant_id, export_id)
    out = await _wait_for_lock(res, caller.tenant_id, export_id)
    if out.reject_reason == "declared_hash_mismatch":
        detail = out.reject_detail or {}
        raise ApiError(
            422, "declared_hash_mismatch",
            "the SHA-256 declared at upload does not match the uploaded bytes; the export was kept"
            " as locked evidence and rejected, and nothing in it will be processed",
            {"export_id": str(export_id), "declared_sha256": detail.get("declared_sha256"),
             "sha256": detail.get("sha256")},
        )  # fmt: skip
    return out


@router.get("/exports/{export_id}", response_model=ExportOut, openapi_extra=perm(P.CONNECTION_READ))
async def get_export(export_id: uuid.UUID, caller: CallerDep, res: ResourcesDep) -> ExportOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_export(s, caller, P.CONNECTION_READ, export_id)
        return await _out(s, res, export_id)


@router.get(
    "/clients/{client_id}/exports", response_model=Page[ExportOut],
    openapi_extra=perm(P.CONNECTION_READ),
)  # fmt: skip
async def list_exports(
    client_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[ExportOut]:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CONNECTION_READ, Scope("client", client_id))
        ids: Sequence[uuid.UUID] = (
            (
                await s.execute(
                    text(
                        "SELECT id FROM slack_exports WHERE client_id = :c"
                        " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
                    ),
                    {"c": client_id, "after": decode(cursor), "n": limit + 1},
                )
            )
            .scalars()
            .all()
        )
        rows = [await _out(s, res, i) for i in ids]
    return page_of(rows, limit, lambda x: x.id)


# ------------------------------------------------------------------ completion
async def _parts(s: AsyncSession, export_id: uuid.UUID) -> list[tuple[int, int, str, str]]:
    rows = (
        await s.execute(
            text(
                "SELECT part_number, size_bytes, sha256, etag FROM export_upload_parts"
                " WHERE export_id = :i ORDER BY part_number"
            ),
            {"i": export_id},
        )
    ).all()
    return [(r.part_number, r.size_bytes, r.sha256, r.etag) for r in rows]


def _check_parts(parts: list[tuple[int, int, str, str]], declared: int, part_min: int) -> None:
    if not parts:
        raise unprocessable("no parts were uploaded")
    numbers = [p[0] for p in parts]
    if numbers != list(range(1, len(parts) + 1)):
        missing = sorted(set(range(1, numbers[-1] + 1)) - set(numbers))
        raise unprocessable(f"parts must be numbered 1..n without gaps; missing {missing[:20]}")
    small = [p[0] for p in parts[:-1] if p[1] < part_min]
    if small:
        raise unprocessable(
            f"every part but the last needs >= {part_min} bytes; too small: {small[:20]}"
        )
    total = sum(p[1] for p in parts)
    if total != declared:
        raise unprocessable(f"received {total} bytes, declared {declared}")


async def _check_stored_parts(
    res: Resources, key: str, upload_id: str, parts: list[tuple[int, int, str, str]]
) -> None:
    """The store must hold exactly the recorded parts (a concurrent re-send of a part with a different
    body could otherwise leave the store and our records disagreeing)."""
    stored: dict[int, tuple[int, str]] = {}
    marker = 0
    while True:
        try:
            page = await res.s3.list_parts(
                Bucket=res.settings.s3_staging_bucket, Key=key, UploadId=upload_id,
                PartNumberMarker=marker,
            )  # fmt: skip
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) == "NoSuchUpload":
                raise ApiError(
                    410, "upload_expired", "this upload expired; start a new export"
                ) from exc
            raise
        for p in page.get("Parts", []):
            stored[p["PartNumber"]] = (p["Size"], p["ETag"])
        if not page.get("IsTruncated"):
            break
        marker = int(page["NextPartNumberMarker"])
    for number, size, _sha, etag in parts:
        if stored.get(number) != (size, etag):
            raise conflict(f"part {number} changed after it was recorded; send it again")


async def _start(res: Resources, tenant_id: uuid.UUID, export_id: uuid.UUID) -> None:
    """Idempotent: a running workflow is reused; a finished one may run again (it reads the export's
    state and does only what is left)."""
    s = res.settings
    await res.temporal.start_workflow(
        "ExportIngestWorkflow",
        ExportRef(
            str(tenant_id),
            str(export_id),
            heartbeat_timeout_seconds=s.activity_heartbeat_timeout_seconds,
            retry_initial_seconds=s.activity_retry_initial_seconds,
            retry_max_seconds=s.activity_retry_max_seconds,
            max_attempts=s.activity_max_attempts,
        ),
        id=export_workflow_id(str(export_id)),
        task_queue=EXPORTS_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )  # fmt: skip


async def _wait_for_lock(res: Resources, tenant_id: uuid.UUID, export_id: uuid.UUID) -> ExportOut:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + res.settings.export_complete_wait_seconds
    while True:
        async with tenant_tx(res.sessions, tenant_id) as s:
            out = await _out(s, res, export_id)
        if out.status != "locking" or loop.time() >= deadline:
            return out
        await asyncio.sleep(0.2)
