"""Collection jobs: create (Idempotency-Key), list, status, cancel, resume, rerun, units,
reconciliation, custody verification, and audited evidence content reads (ADR 0013).

Every job action is a custody event in the job's stream naming the acting principal, the request id and
(for creations) the idempotency key. ``completed_unverified`` is never presented as clean (ADR 0005).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import Caller
from edisc_api.authz import P, Permission, Scope, authorize, perm
from edisc_api.errors import conflict, not_found, unprocessable
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, decode_text, encode_text, page_of
from edisc_api.routes.hierarchy import ensure_matter_open
from edisc_connector_dummy.guard import source_permitted
from edisc_connector_slack_export.archive_access import open_archive_entry
from edisc_connectors_base.types import CollectionScope, ThreadParentPolicy
from edisc_core.canonical import canonical_json
from edisc_core.ids import new_id
from edisc_core.schemas import ARCHIVE_CAVEAT, JobStatus, ReconStatus, ScopeType
from edisc_core.time import UtcDatetime
from edisc_custody.log import anchor_if_due, append, verify_chain
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_worker.contracts import JobInput, RunConfig, task_queue
from edisc_worker.pipeline import Pipeline
from edisc_worker.workflows import CollectionJobWorkflow

router = APIRouter(prefix="/v1")

CLEAN = {JobStatus.COMPLETED.value}
MATCHED = {ReconStatus.MATCHED.value, ReconStatus.MATCHED_AGAINST_ARCHIVE.value}


def clean_basis(status: str) -> str | None:
    if status == JobStatus.COMPLETED.value:
        return "source"
    if status == JobStatus.COMPLETED_AGAINST_ARCHIVE.value:
        return "archive"
    return None


def caveat(status: str) -> str | None:
    return ARCHIVE_CAVEAT if status == JobStatus.COMPLETED_AGAINST_ARCHIVE.value else None


IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeIn(Strict):
    type: Literal["channel", "custodian"]
    external_id: str = Field(min_length=1, max_length=200)
    date_from: UtcDatetime
    date_to: UtcDatetime
    thread_parent_policy: ThreadParentPolicy = ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD

    @model_validator(mode="after")
    def _range(self) -> ScopeIn:
        if self.date_from >= self.date_to:
            raise ValueError("date_from must be before date_to")
        return self


class JobIn(Strict):
    connection_id: uuid.UUID
    scopes: list[ScopeIn] = Field(min_length=1, max_length=100)
    workspace_id: uuid.UUID | None = None


class ScopeOut(Strict):
    type: str
    external_id: str
    date_from: datetime
    date_to: datetime
    thread_parent_policy: str


class JobOut(Strict):
    id: uuid.UUID
    matter_id: uuid.UUID
    connection_id: uuid.UUID
    workspace_id: uuid.UUID | None
    status: str
    clean: (
        bool  # only "completed" is clean; completed_unverified / with_gaps / failed units never are
    )
    # what completeness was checked against: "source" (the live source's counts) or "archive" (an
    # uploaded export only); None while running or when not complete
    clean_basis: str | None
    caveat: str | None  # verbatim (ADR 0014) whenever the basis is the archive
    requested_by: str
    rerun_of: uuid.UUID | None
    created_at: datetime
    finished_at: datetime | None
    sealed: bool
    paused_ms: int | None
    units: dict[str, int]
    scopes: list[ScopeOut]


class UnitOut(Strict):
    unit_key: str
    kind: str
    status: str
    recon_status: str
    expected_count: int | None
    collected_count: int
    file_gaps: int
    last_error: str | None
    day_anomalies: int
    caveat: str | None  # verbatim (ADR 0014) when recon_status is matched_against_archive

    @model_validator(mode="before")
    @classmethod
    def _caveat(cls, data: Any) -> Any:
        if isinstance(data, dict) and "caveat" not in data:
            archive = data.get("recon_status") == ReconStatus.MATCHED_AGAINST_ARCHIVE.value
            data = {**data, "caveat": ARCHIVE_CAVEAT if archive else None}
        return data


class ReconciliationOut(Strict):
    job_id: uuid.UUID
    status: str
    clean: bool
    clean_basis: str | None
    caveat: str | None
    by_recon_status: dict[str, int]
    not_matched: list[UnitOut]


class VerifyOut(Strict):
    ok: bool
    events: int
    batches_checked: int
    items_checked: int
    anchors_checked: int
    errors: list[str]


# ------------------------------------------------------------------ helpers
def _pipeline(res: Resources, source: str = "dummy") -> Pipeline:
    return Pipeline(res.sessions, res.s3, res.settings, res.connectors[source])


async def _job_matter(s: AsyncSession, job_id: uuid.UUID) -> uuid.UUID:
    matter: uuid.UUID | None = (
        await s.execute(text("SELECT matter_id FROM collection_jobs WHERE id = :j"), {"j": job_id})
    ).scalar_one_or_none()
    if matter is None:
        raise not_found()
    return matter


async def _authorize_job(
    s: AsyncSession, caller: Caller, permission: Permission, job_id: uuid.UUID
) -> None:
    await authorize(s, caller, permission, Scope("matter", await _job_matter(s, job_id)))


async def _job_out(s: AsyncSession, job_id: uuid.UUID) -> JobOut:
    job = (
        await s.execute(
            text(
                "SELECT id, matter_id, connection_id, workspace_id, status, requested_by, rerun_of, created_at,"
                " finished_at, sealed_at, status_detail FROM collection_jobs WHERE id = :j"
            ),
            {"j": job_id},
        )
    ).one()
    units = {
        r.k: r.n
        for r in (
            await s.execute(
                text(
                    "SELECT status AS k, count(*) AS n FROM work_units WHERE job_id = :j"
                    " AND kind = 'conversation_day' GROUP BY status"
                ),
                {"j": job_id},
            )
        ).all()
    }
    scopes = (
        await s.execute(
            text(
                "SELECT scope_type, external_id, date_from, date_to, thread_parent_policy"
                " FROM collection_scopes WHERE job_id = :j ORDER BY date_from, external_id, id"
            ),
            {"j": job_id},
        )
    ).all()
    detail: dict[str, Any] = job.status_detail or {}
    return JobOut(
        id=job.id,
        matter_id=job.matter_id,
        connection_id=job.connection_id,
        workspace_id=job.workspace_id,
        status=job.status,
        clean=job.status in CLEAN,
        clean_basis=clean_basis(job.status),
        caveat=caveat(job.status),
        requested_by=job.requested_by,
        rerun_of=job.rerun_of,
        created_at=job.created_at,
        finished_at=job.finished_at,
        sealed=job.sealed_at is not None,
        paused_ms=detail.get("paused_ms"),
        units=units,
        scopes=[
            ScopeOut(
                type=r.scope_type,
                external_id=r.external_id,
                date_from=r.date_from,
                date_to=r.date_to,
                thread_parent_policy=r.thread_parent_policy,
            )
            for r in scopes
        ],
    )


def _request_hash(matter_id: uuid.UUID, body: BaseModel | None, action: str) -> str:
    doc = {
        "action": action,
        "matter": str(matter_id),
        "body": body.model_dump(mode="json") if body else None,
    }
    return hashlib.sha256(canonical_json(doc)).hexdigest()


async def _claim_key(
    s: AsyncSession, caller: Caller, key: str, request_hash: str
) -> uuid.UUID | None:
    """Claim an Idempotency-Key inside the caller's transaction. Returns the job of an earlier request
    with the same key and body (replay), None when this request owns the key. A concurrent duplicate
    blocks on the unique key until the first commits, then replays its job."""
    inserted = (
        await s.execute(
            text(
                "INSERT INTO api_idempotency (tenant_id, key, principal_id, request_hash)"
                " VALUES (:t, :k, :p, :h) ON CONFLICT DO NOTHING RETURNING key"
            ),
            {"t": caller.tenant_id, "k": key, "p": caller.principal_id, "h": request_hash},
        )
    ).first()
    if inserted is not None:
        return None
    row = (
        await s.execute(
            text("SELECT request_hash, job_id FROM api_idempotency WHERE key = :k"), {"k": key}
        )
    ).one()
    if row.request_hash != request_hash:
        raise unprocessable("Idempotency-Key was already used with a different request")
    if row.job_id is None:  # cannot happen: the key and its job commit together
        raise conflict("the original request with this Idempotency-Key did not complete")
    job_id: uuid.UUID = row.job_id
    return job_id


async def _ensure_workflow(
    res: Resources, tenant_id: uuid.UUID, job_id: uuid.UUID, source: str
) -> None:
    """Start the job's workflow once (id = job id, never reused). A replayed request after a crash
    between commit and start starts it; otherwise "already started" is the expected answer."""
    try:
        await res.temporal.start_workflow(
            CollectionJobWorkflow.run,
            JobInput(str(tenant_id), str(job_id), RunConfig.from_settings(res.settings)),
            id=str(job_id),
            task_queue=task_queue(source),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
    except WorkflowAlreadyStartedError:
        return


async def _custody(
    res: Resources, caller: Caller, job_id: uuid.UUID, event_type: str, payload: dict[str, Any]
) -> None:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await append(
            s,
            tenant_id=caller.tenant_id,
            stream_id=job_id,
            job_id=job_id,
            event_type=event_type,
            actor=caller.actor,
            payload=payload,
        )
    await anchor_if_due(
        res.sessions, res.s3, res.settings, tenant_id=caller.tenant_id, stream_id=job_id
    )


async def _signal(res: Resources, job_id: uuid.UUID, signal: str) -> None:
    try:
        await res.temporal.get_workflow_handle(str(job_id)).signal(signal)
    except RPCError as exc:
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise


# ------------------------------------------------------------------ create / list / get
@router.post(
    "/matters/{matter_id}/jobs",
    status_code=201,
    response_model=JobOut,
    openapi_extra=perm(P.JOB_START),
)
async def create_job(
    matter_id: uuid.UUID,
    body: JobIn,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
    idempotency_key: IdempotencyKey = None,
) -> JobOut:
    job_id = new_id()
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.JOB_START, Scope("matter", matter_id))
        await ensure_matter_open(s, matter_id)
        conn = (
            await s.execute(
                text(
                    "SELECT c.source, c.status FROM connections c JOIN matters m ON m.client_id = c.client_id"
                    " WHERE c.id = :c AND m.id = :m"
                ),
                {"c": body.connection_id, "m": matter_id},
            )
        ).one_or_none()
        if conn is None:
            raise unprocessable("connection_id is not a connection of this matter's client")
        if conn.status != "active":
            raise conflict(f"connection is {conn.status}")
        connector = res.connectors.get(conn.source)
        if connector is None or not source_permitted(res.settings.env, conn.source):
            raise unprocessable(f"no connector for {conn.source}")
        if connector.archive_backed and any(sc.type != "channel" for sc in body.scopes):
            raise unprocessable(
                "export collections take channel scopes only (no membership history)"
            )
        if body.workspace_id is not None:
            ws = (
                await s.execute(
                    text("SELECT 1 FROM workspaces WHERE id = :w AND matter_id = :m"),
                    {"w": body.workspace_id, "m": matter_id},
                )
            ).first()
            if ws is None:
                raise unprocessable("workspace_id is not a workspace of this matter")
        replay = None
        if idempotency_key:
            replay = await _claim_key(
                s, caller, idempotency_key, _request_hash(matter_id, body, "create")
            )
        if replay is None:
            await _pipeline(res, conn.source).create_job(
                s,
                tenant_id=caller.tenant_id,
                job_id=job_id,
                matter_id=matter_id,
                connection_id=body.connection_id,
                scopes=[
                    CollectionScope(
                        ScopeType(sc.type),
                        sc.external_id,
                        sc.date_from,
                        sc.date_to,
                        sc.thread_parent_policy,
                    )
                    for sc in body.scopes
                ],
                requested_by=caller.actor,
                workspace_id=body.workspace_id,
                context={
                    "request_id": rid,
                    **({"idempotency_key": idempotency_key} if idempotency_key else {}),
                },
            )
            if idempotency_key:
                await s.execute(
                    text("UPDATE api_idempotency SET job_id = :j WHERE key = :k"),
                    {"j": job_id, "k": idempotency_key},
                )
        else:
            job_id = replay
    await anchor_if_due(
        res.sessions, res.s3, res.settings, tenant_id=caller.tenant_id, stream_id=job_id
    )
    await _ensure_workflow(res, caller.tenant_id, job_id, conn.source)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        return await _job_out(s, job_id)


@router.get(
    "/matters/{matter_id}/jobs", response_model=Page[JobOut], openapi_extra=perm(P.JOB_READ)
)
async def list_jobs(
    matter_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[JobOut]:
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.JOB_READ, Scope("matter", matter_id))
        ids: list[uuid.UUID] = list(
            (
                await s.execute(
                    text(
                        "SELECT id FROM collection_jobs WHERE matter_id = :m"
                        " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
                    ),
                    {"m": matter_id, "after": after, "n": limit + 1},
                )
            )
            .scalars()
            .all()
        )
        jobs = [await _job_out(s, j) for j in ids]
    return page_of(jobs, limit, lambda j: j.id)


@router.get("/jobs/{job_id}", response_model=JobOut, openapi_extra=perm(P.JOB_READ))
async def get_job(job_id: uuid.UUID, caller: CallerDep, res: ResourcesDep) -> JobOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_job(s, caller, P.JOB_READ, job_id)
        return await _job_out(s, job_id)


# ------------------------------------------------------------------ actions
@router.post("/jobs/{job_id}/cancel", response_model=JobOut, openapi_extra=perm(P.JOB_CANCEL))
async def cancel_job(
    job_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> JobOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_job(s, caller, P.JOB_CANCEL, job_id)
        status: str = (
            await s.execute(text("SELECT status FROM collection_jobs WHERE id = :j"), {"j": job_id})
        ).scalar_one()
    if JobStatus(status).is_terminal:
        raise conflict(f"job is already {status}")
    # the custody event names the user; the workflow's own stop request is then a no-op (first wins)
    await _pipeline(res).request_stop(
        tenant_id=caller.tenant_id, job_id=job_id, reason="cancel",
        detail=f"cancelled by {caller.actor} (request {rid})", actor=caller.actor,
    )  # fmt: skip
    await _signal(res, job_id, "cancel")
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        return await _job_out(s, job_id)


@router.post("/jobs/{job_id}/resume", response_model=JobOut, openapi_extra=perm(P.JOB_RESUME))
async def resume_job(
    job_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> JobOut:
    """Ask the job to re-check its state now (e.g. after retry_later cool-downs or an operator fix). A
    job paused for re-authorization resumes only through the connection's re-authorization."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_job(s, caller, P.JOB_RESUME, job_id)
        job = (
            await s.execute(
                text(
                    "SELECT j.status, c.status AS connection_status FROM collection_jobs j"
                    " JOIN connections c ON c.id = j.connection_id WHERE j.id = :j"
                ),
                {"j": job_id},
            )
        ).one()
    if JobStatus(job.status).is_terminal:
        raise conflict(f"job is already {job.status}")
    if job.status == JobStatus.PAUSED_AWAITING_REAUTH.value:
        raise conflict("the connection needs re-authorization (POST /v1/connections/{id}/reauth)")
    await _custody(res, caller, job_id, "resume_requested", {"request_id": rid})
    await _signal(res, job_id, "wake")
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        return await _job_out(s, job_id)


@router.post(
    "/jobs/{job_id}/rerun", status_code=201, response_model=JobOut, openapi_extra=perm(P.JOB_RERUN)
)
async def rerun_job(
    job_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
    idempotency_key: IdempotencyKey = None,
) -> JobOut:
    """Re-run the failed units of a finished job as a NEW job (the sealed original stays closed)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        matter = await _job_matter(s, job_id)
        await authorize(s, caller, P.JOB_RERUN, Scope("matter", matter))
        job = (
            await s.execute(
                text(
                    "SELECT j.status, j.sealed_at, c.source, c.status AS connection_status,"
                    " (SELECT count(*) FROM work_units w WHERE w.job_id = j.id AND w.status = 'failed') AS failed"
                    " FROM collection_jobs j JOIN connections c ON c.id = j.connection_id WHERE j.id = :j"
                ),
                {"j": job_id},
            )
        ).one()
        if job.sealed_at is None:
            raise conflict("only a finished (sealed) job can be re-run")
        if job.failed == 0:
            raise conflict("the job has no failed units")
        if job.connection_status != "active":
            raise conflict(f"connection is {job.connection_status}")
        if idempotency_key:
            replay = await _claim_key(
                s, caller, idempotency_key, _request_hash(matter, None, f"rerun:{job_id}")
            )
            if replay is not None:
                new_job = replay
    if not idempotency_key or replay is None:
        new_job = await _pipeline(res, job.source).create_rerun_job(
            tenant_id=caller.tenant_id, original_job_id=job_id, requested_by=caller.actor
        )
        if idempotency_key:
            async with tenant_tx(res.sessions, caller.tenant_id) as s:
                await s.execute(
                    text("UPDATE api_idempotency SET job_id = :j WHERE key = :k"),
                    {"j": new_job, "k": idempotency_key},
                )
        await _custody(
            res, caller, new_job, "rerun_requested", {"request_id": rid, "rerun_of": str(job_id)}
        )
    await _ensure_workflow(res, caller.tenant_id, new_job, job.source)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        return await _job_out(s, new_job)


# ------------------------------------------------------------------ units, reconciliation, custody
UNIT_COLUMNS = (
    "SELECT unit_key, kind, status, recon_status, expected_count, collected_count, file_gaps, last_error,"
    " day_anomalies FROM work_units WHERE job_id = :j"
)


@router.get("/jobs/{job_id}/units", response_model=Page[UnitOut], openapi_extra=perm(P.JOB_READ))
async def list_units(
    job_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[UnitOut]:
    after = decode_text(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_job(s, caller, P.JOB_READ, job_id)
        rows = (
            await s.execute(
                text(
                    UNIT_COLUMNS
                    + " AND (CAST(:after AS text) IS NULL OR unit_key > :after) ORDER BY unit_key LIMIT :n"
                ),
                {"j": job_id, "after": after, "n": limit + 1},
            )
        ).all()
    units = [UnitOut(**r._mapping) for r in rows]
    if len(units) > limit:
        return Page[UnitOut](
            items=units[:limit], next_cursor=encode_text(units[limit - 1].unit_key)
        )
    return Page[UnitOut](items=units, next_cursor=None)


@router.get(
    "/jobs/{job_id}/reconciliation",
    response_model=ReconciliationOut,
    openapi_extra=perm(P.JOB_READ),
)
async def reconciliation(
    job_id: uuid.UUID, caller: CallerDep, res: ResourcesDep
) -> ReconciliationOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_job(s, caller, P.JOB_READ, job_id)
        status: str = (
            await s.execute(text("SELECT status FROM collection_jobs WHERE id = :j"), {"j": job_id})
        ).scalar_one()
        rows = (
            await s.execute(
                text(UNIT_COLUMNS + " AND kind = 'conversation_day' ORDER BY unit_key"),
                {"j": job_id},
            )
        ).all()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.recon_status] = counts.get(r.recon_status, 0) + 1
    return ReconciliationOut(
        job_id=job_id,
        status=status,
        clean=status in CLEAN,
        clean_basis=clean_basis(status),
        caveat=caveat(status),
        by_recon_status=counts,
        not_matched=[UnitOut(**r._mapping) for r in rows if r.recon_status not in MATCHED][:500],
    )


@router.get(
    "/jobs/{job_id}/custody/verify", response_model=VerifyOut, openapi_extra=perm(P.CUSTODY_READ)
)
async def verify(job_id: uuid.UUID, caller: CallerDep, res: ResourcesDep) -> VerifyOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_job(s, caller, P.CUSTODY_READ, job_id)
    report = await verify_chain(
        res.sessions, res.s3, res.settings, tenant_id=caller.tenant_id, stream_id=job_id
    )
    return VerifyOut(
        ok=report.ok,
        events=report.events,
        batches_checked=report.batches_checked,
        items_checked=report.items_checked,
        anchors_checked=report.anchors_checked,
        errors=report.errors[:100],
    )


# ------------------------------------------------------------------ evidence content (always audited)
@router.get("/evidence/{evidence_id}/content", openapi_extra=perm(P.EVIDENCE_READ))
async def evidence_content(
    evidence_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
    purpose: Annotated[Literal["preview", "download", "export", "rsmf"], Query()],
) -> StreamingResponse:
    """The pinned evidence bytes. The audit event is committed BEFORE any byte is returned (ADR 0013
    decision d): a read that is not recorded never happens."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT e.kind, e.sha256, e.size_bytes, e.job_id, j.matter_id FROM evidence_objects e"
                    " LEFT JOIN collection_jobs j ON j.id = e.job_id WHERE e.id = :e AND e.state = 'complete'"
                ),
                {"e": evidence_id},
            )
        ).one_or_none()
        if row is None or row.matter_id is None:
            raise not_found()
        await authorize(s, caller, P.EVIDENCE_READ, Scope("matter", row.matter_id))
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="evidence_content_read",
            payload={"evidence_id": str(evidence_id), "sha256": row.sha256, "size": row.size_bytes,
                     "purpose": purpose, "matter_id": str(row.matter_id), "job_id": str(row.job_id)},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    writer = EvidenceWriter(res.sessions, res.s3, res.settings)

    async def body() -> AsyncIterator[bytes]:
        if row.kind == "archive_entry":  # decompressed from the locked export's pinned version
            chunks = open_archive_entry(
                res.sessions,
                res.s3,
                res.settings,
                tenant_id=caller.tenant_id,
                evidence_id=evidence_id,
            )
        else:
            chunks = writer.open(tenant_id=caller.tenant_id, evidence_id=evidence_id)
        async for chunk in chunks:
            yield chunk

    return StreamingResponse(
        body(),
        media_type="application/json"
        if row.kind in ("page", "archive_entry")
        else "application/octet-stream",
        headers={"x-evidence-sha256": row.sha256, "cache-control": "no-store"},
    )
