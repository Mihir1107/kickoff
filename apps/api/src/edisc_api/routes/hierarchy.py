"""Clients, matters and workspaces (ADR 0013 section 1). Every change is an audit event."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import Caller
from edisc_api.authz import TENANT, P, Scope, authorize, perm, permissions, roles_at, visible_ids
from edisc_api.errors import conflict, forbidden, not_found, unprocessable
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, page_of
from edisc_core.ids import new_id
from edisc_core.time import UtcDatetime, utc_now
from edisc_db.session import tenant_tx
from edisc_worker.contracts import MAINTENANCE_QUEUE

router = APIRouter(prefix="/v1")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientIn(Strict):
    name: str = Field(min_length=1, max_length=200)


class ClientOut(Strict):
    id: uuid.UUID
    name: str
    is_default: bool
    created_at: datetime
    closed_at: datetime | None


class MatterIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    retention_until: UtcDatetime


class MatterOut(Strict):
    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    retention_until: datetime
    created_at: datetime
    closed_at: datetime | None


class WorkspaceIn(Strict):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceOut(Strict):
    id: uuid.UUID
    matter_id: uuid.UUID
    name: str
    created_at: datetime


CLIENT_ONE = "SELECT id, name, is_default, created_at, closed_at FROM clients WHERE id = :i"
MATTER_ONE = (
    "SELECT id, client_id, name, retention_until, created_at, closed_at FROM matters WHERE id = :i"
)


async def ensure_client_open(s: AsyncSession, client_id: uuid.UUID) -> None:
    closed = (
        await s.execute(text("SELECT closed_at FROM clients WHERE id = :i"), {"i": client_id})
    ).scalar_one_or_none()
    if closed is not None:
        raise conflict("the client is closed")


async def ensure_matter_open(s: AsyncSession, matter_id: uuid.UUID) -> None:
    closed = (
        await s.execute(text("SELECT closed_at FROM matters WHERE id = :i"), {"i": matter_id})
    ).scalar_one_or_none()
    if closed is not None:
        raise conflict("the matter is closed")


# ------------------------------------------------------------------ clients
@router.post(
    "/clients", status_code=201, response_model=ClientOut, openapi_extra=perm(P.CLIENT_CREATE)
)
async def create_client(
    body: ClientIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> ClientOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CLIENT_CREATE, TENANT)
        row = (
            await s.execute(
                text(
                    "INSERT INTO clients (id, tenant_id, name) VALUES (:i, :t, :n)"
                    " RETURNING id, name, is_default, created_at, closed_at"
                ),
                {"i": new_id(), "t": caller.tenant_id, "n": body.name},
            )
        ).one()
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="client_created",
            payload={"client_id": str(row.id), "name": body.name}, request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return ClientOut(**row._mapping)


@router.get("/clients", response_model=Page[ClientOut], openapi_extra=perm("visible"))
async def list_clients(
    caller: CallerDep, res: ResourcesDep, cursor: CursorQ = None, limit: LimitQ = 50
) -> Page[ClientOut]:
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        visible = await visible_ids(s, caller, "client")
        rows = (
            await s.execute(
                text(
                    "SELECT id, name, is_default, created_at, closed_at FROM clients"
                    " WHERE (CAST(:after AS uuid) IS NULL OR id > :after)"
                    " AND (CAST(:all AS boolean) OR id = ANY(:ids)) ORDER BY id LIMIT :n"
                ),
                {
                    "after": after,
                    "all": visible is None,
                    "ids": list(visible or ()),
                    "n": limit + 1,
                },
            )
        ).all()
    return page_of([ClientOut(**r._mapping) for r in rows], limit, lambda c: c.id)


@router.get("/clients/{client_id}", response_model=ClientOut, openapi_extra=perm(P.CLIENT_READ))
async def get_client(client_id: uuid.UUID, caller: CallerDep, res: ResourcesDep) -> ClientOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CLIENT_READ, Scope("client", client_id))
        row = (
            await s.execute(
                text(CLIENT_ONE),
                {"i": client_id},
            )
        ).one()
    return ClientOut(**row._mapping)


# ------------------------------------------------------------------ matters
@router.post(
    "/clients/{client_id}/matters", status_code=201, response_model=MatterOut,
    openapi_extra=perm(P.MATTER_CREATE),
)  # fmt: skip
async def create_matter(
    client_id: uuid.UUID, body: MatterIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> MatterOut:
    if body.retention_until <= utc_now():
        raise unprocessable("retention_until must be in the future")
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.MATTER_CREATE, Scope("client", client_id))
        await ensure_client_open(s, client_id)
        row = (
            await s.execute(
                text(
                    "INSERT INTO matters (id, tenant_id, client_id, name, retention_until)"
                    " VALUES (:i, :t, :c, :n, :r) RETURNING id, client_id, name, retention_until, created_at, closed_at"
                ),
                {
                    "i": new_id(),
                    "t": caller.tenant_id,
                    "c": client_id,
                    "n": body.name,
                    "r": body.retention_until,
                },
            )
        ).one()
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="matter_created",
            payload={"matter_id": str(row.id), "client_id": str(client_id), "name": body.name,
                     "retention_until": body.retention_until.isoformat()},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return MatterOut(**row._mapping)


@router.get(
    "/clients/{client_id}/matters", response_model=Page[MatterOut], openapi_extra=perm("visible")
)
async def list_matters(
    client_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[MatterOut]:
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        clients = await visible_ids(s, caller, "client")
        if clients is not None and client_id not in clients:
            raise not_found()
        matters = await visible_ids(s, caller, "matter")
        client_wide = clients is None or bool(
            (
                await s.execute(
                    text(
                        "SELECT 1 FROM role_assignments WHERE revoked_at IS NULL AND scope_type = 'client'"
                        " AND scope_id = :c AND (principal_id = :p OR group_id = ANY(:g))"
                    ),
                    {"c": client_id, "p": caller.principal_id, "g": list(caller.group_ids)},
                )
            ).first()
        )
        rows = (
            await s.execute(
                text(
                    "SELECT id, client_id, name, retention_until, created_at, closed_at FROM matters WHERE client_id = :c"
                    " AND (CAST(:after AS uuid) IS NULL OR id > :after)"
                    " AND (CAST(:all AS boolean) OR id = ANY(:ids)) ORDER BY id LIMIT :n"
                ),
                {
                    "c": client_id,
                    "after": after,
                    "all": client_wide,
                    "ids": list(matters or ()),
                    "n": limit + 1,
                },
            )
        ).all()
    return page_of([MatterOut(**r._mapping) for r in rows], limit, lambda m: m.id)


@router.get("/matters/{matter_id}", response_model=MatterOut, openapi_extra=perm(P.MATTER_READ))
async def get_matter(matter_id: uuid.UUID, caller: CallerDep, res: ResourcesDep) -> MatterOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.MATTER_READ, Scope("matter", matter_id))
        row = (
            await s.execute(
                text(MATTER_ONE),
                {"i": matter_id},
            )
        ).one()
    return MatterOut(**row._mapping)


# ------------------------------------------------------------------ closing (retention ownership, ADR 0002)
@router.post(
    "/matters/{matter_id}/close", response_model=MatterOut, openapi_extra=perm(P.MATTER_CREATE)
)
async def close_matter(
    matter_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> MatterOut:
    """No new jobs; the retention-extension job stops extending this matter's evidence, which then
    expires on schedule. Running jobs must be finished or cancelled first (and, once legal hold exists,
    a held matter cannot close). Reversible by a tenant admin (``.../reopen``)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.MATTER_CREATE, Scope("matter", matter_id))
        await ensure_matter_open(s, matter_id)
        running: int = (
            await s.execute(
                text(
                    "SELECT count(*) FROM collection_jobs WHERE matter_id = :m"
                    " AND status IN ('pending', 'running', 'paused_awaiting_reauth')"
                ),
                {"m": matter_id},
            )
        ).scalar_one()
        if running:
            raise conflict(f"{running} job(s) of this matter are still running")
        await s.execute(
            text("UPDATE matters SET closed_at = now(), closed_by = :by WHERE id = :i"),
            {"by": caller.actor, "i": matter_id},
        )
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="matter_closed",
            payload={"matter_id": str(matter_id)}, request_id=rid,
        )  # fmt: skip
        row = (await s.execute(text(MATTER_ONE), {"i": matter_id})).one()
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return MatterOut(**row._mapping)


@router.post(
    "/clients/{client_id}/close", response_model=ClientOut, openapi_extra=perm(P.CLIENT_CREATE)
)
async def close_client(
    client_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> ClientOut:
    """Its matters must be closed first. Its validated exports stop being extended. Reversible by a
    tenant admin (``.../reopen``)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CLIENT_CREATE, Scope("client", client_id))
        await ensure_client_open(s, client_id)
        row = (await s.execute(text(CLIENT_ONE), {"i": client_id})).one()
        if row.is_default:
            raise conflict("the default client cannot be closed")
        open_matters: int = (
            await s.execute(
                text("SELECT count(*) FROM matters WHERE client_id = :c AND closed_at IS NULL"),
                {"c": client_id},
            )
        ).scalar_one()
        if open_matters:
            raise conflict(f"{open_matters} matter(s) of this client are still open")
        await s.execute(
            text("UPDATE clients SET closed_at = now(), closed_by = :by WHERE id = :i"),
            {"by": caller.actor, "i": client_id},
        )
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="client_closed",
            payload={"client_id": str(client_id)}, request_id=rid,
        )  # fmt: skip
        row = (await s.execute(text(CLIENT_ONE), {"i": client_id})).one()
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return ClientOut(**row._mapping)


class ReopenIn(Strict):
    # a matter whose retention date has passed needs a new one to protect anything again
    retention_until: UtcDatetime | None = None


class RetentionGapOut(Strict):
    id: uuid.UUID
    evidence_object_id: uuid.UUID
    owner_type: str
    owner_id: uuid.UUID
    unprotected_from: datetime
    unprotected_until: datetime
    outcome: str
    created_at: datetime


async def _require_tenant_admin(s: AsyncSession, caller: Caller) -> None:
    if P.TENANT_ADMIN not in permissions(await roles_at(s, caller, [TENANT])):
        raise forbidden("reopening needs a tenant admin")


async def _start_retention_run(res: Resources, tenant_id: uuid.UUID) -> None:
    """Re-lock what lapsed while closed now, not at the next scheduled sweep. A run already in progress
    may have passed this owner before the reopen committed, so it is replaced (the run is idempotent)."""
    await res.temporal.start_workflow(
        "TenantRetentionWorkflow",
        str(tenant_id),
        id=f"retention-{tenant_id}",
        task_queue=MAINTENANCE_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.TERMINATE_EXISTING,
    )


@router.post(
    "/matters/{matter_id}/reopen", response_model=MatterOut, openapi_extra=perm(P.TENANT_ADMIN)
)
async def reopen_matter(
    matter_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
    body: ReopenIn | None = None,
) -> MatterOut:
    """Tenant admins only, audited. Retention extension resumes at once; evidence whose retention lapsed
    while closed is re-locked if it still exists, and every unprotected window is recorded
    (``GET .../retention-gaps``)."""
    body = body or ReopenIn()
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.MATTER_READ, Scope("matter", matter_id))
        await _require_tenant_admin(s, caller)
        row = (await s.execute(text(MATTER_ONE), {"i": matter_id})).one()
        if row.closed_at is None:
            raise conflict("the matter is not closed")
        await ensure_client_open(s, row.client_id)
        until = row.retention_until
        if body.retention_until is not None:
            if body.retention_until < row.retention_until:
                raise unprocessable("retention_until may only be extended")
            until = body.retention_until
        if until <= utc_now():
            raise conflict(
                "the matter's retention date has passed; reopen with a new retention_until"
            )
        closed = (await s.execute(
            text("SELECT closed_at, closed_by FROM matters WHERE id = :i"), {"i": matter_id}
        )).one()  # fmt: skip
        await s.execute(
            text(
                "UPDATE matters SET closed_at = NULL, closed_by = NULL, retention_until = :r"
                " WHERE id = :i"
            ),
            {"r": until, "i": matter_id},
        )
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="matter_reopened",
            payload={"matter_id": str(matter_id), "closed_at": closed.closed_at.isoformat(),
                     "closed_by": closed.closed_by, "retention_until": until.isoformat()},
            request_id=rid,
        )  # fmt: skip
        row = (await s.execute(text(MATTER_ONE), {"i": matter_id})).one()
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    await _start_retention_run(res, caller.tenant_id)
    return MatterOut(**row._mapping)


@router.post(
    "/clients/{client_id}/reopen", response_model=ClientOut, openapi_extra=perm(P.TENANT_ADMIN)
)
async def reopen_client(
    client_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> ClientOut:
    """Tenant admins only, audited. Its matters stay closed until reopened themselves; its validated
    exports are protected again at once (lapsed ones re-locked if they still exist, gaps recorded)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CLIENT_READ, Scope("client", client_id))
        await _require_tenant_admin(s, caller)
        closed = (await s.execute(
            text("SELECT closed_at, closed_by FROM clients WHERE id = :i"), {"i": client_id}
        )).one()  # fmt: skip
        if closed.closed_at is None:
            raise conflict("the client is not closed")
        await s.execute(
            text("UPDATE clients SET closed_at = NULL, closed_by = NULL WHERE id = :i"),
            {"i": client_id},
        )
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="client_reopened",
            payload={"client_id": str(client_id), "closed_at": closed.closed_at.isoformat(),
                     "closed_by": closed.closed_by},
            request_id=rid,
        )  # fmt: skip
        row = (await s.execute(text(CLIENT_ONE), {"i": client_id})).one()
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    await _start_retention_run(res, caller.tenant_id)
    return ClientOut(**row._mapping)


async def _gaps(
    s: AsyncSession, owner_id: uuid.UUID, after: uuid.UUID | None, limit: int
) -> Page[RetentionGapOut]:
    rows = (
        await s.execute(
            text(
                "SELECT id, evidence_object_id, owner_type, owner_id, unprotected_from,"
                " unprotected_until, outcome, created_at FROM retention_gaps WHERE owner_id = :o"
                " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
            ),
            {"o": owner_id, "after": after, "n": limit + 1},
        )
    ).all()
    return page_of([RetentionGapOut(**r._mapping) for r in rows], limit, lambda g: g.id)


@router.get(
    "/matters/{matter_id}/retention-gaps", response_model=Page[RetentionGapOut],
    openapi_extra=perm(P.CUSTODY_READ),
)  # fmt: skip
async def matter_retention_gaps(
    matter_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[RetentionGapOut]:
    """Every window in which evidence of this matter was not under retention (ADR 0002)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CUSTODY_READ, Scope("matter", matter_id))
        return await _gaps(s, matter_id, decode(cursor), limit)


@router.get(
    "/clients/{client_id}/retention-gaps", response_model=Page[RetentionGapOut],
    openapi_extra=perm(P.CUSTODY_READ),
)  # fmt: skip
async def client_retention_gaps(
    client_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[RetentionGapOut]:
    """Every window in which a client-level export was not under retention (ADR 0002)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CUSTODY_READ, Scope("client", client_id))
        return await _gaps(s, client_id, decode(cursor), limit)


# ------------------------------------------------------------------ workspaces (modelled; no features yet)
@router.post(
    "/matters/{matter_id}/workspaces", status_code=201, response_model=WorkspaceOut,
    openapi_extra=perm(P.WORKSPACE_CREATE),
)  # fmt: skip
async def create_workspace(
    matter_id: uuid.UUID, body: WorkspaceIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> WorkspaceOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.WORKSPACE_CREATE, Scope("matter", matter_id))
        row = (
            await s.execute(
                text(
                    "INSERT INTO workspaces (id, tenant_id, matter_id, name) VALUES (:i, :t, :m, :n)"
                    " RETURNING id, matter_id, name, created_at"
                ),
                {"i": new_id(), "t": caller.tenant_id, "m": matter_id, "n": body.name},
            )
        ).one()
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="workspace_created",
            payload={"workspace_id": str(row.id), "matter_id": str(matter_id), "name": body.name},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return WorkspaceOut(**row._mapping)


@router.get(
    "/matters/{matter_id}/workspaces", response_model=Page[WorkspaceOut],
    openapi_extra=perm(P.MATTER_READ),
)  # fmt: skip
async def list_workspaces(
    matter_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[WorkspaceOut]:
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.MATTER_READ, Scope("matter", matter_id))
        rows = (
            await s.execute(
                text(
                    "SELECT id, matter_id, name, created_at FROM workspaces WHERE matter_id = :m"
                    " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
                ),
                {"m": matter_id, "after": after, "n": limit + 1},
            )
        ).all()
    return page_of([WorkspaceOut(**r._mapping) for r in rows], limit, lambda w: w.id)
