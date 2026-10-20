"""Legal holds (M18 §1.4, ADR 0019; D1-D4 of docs/plans/m18.md §12).

- Place: ``POST /v1/matters/{id}/legal-holds`` / ``POST /v1/clients/{id}/legal-holds`` (``legal_hold.place``:
  tenant admins, matter managers at the matter or client), a reason, recent sign-in. Effective when
  the transaction commits; the S3 flags follow in the background (``LegalHoldWorkflow``, queue
  ``holds``) and their progress is reported as the store confirms them, never before.
- Release, two people (D3): ``POST /v1/legal-holds/{id}/release`` asks (``legal_hold.release``: tenant
  admins only, a reason, recent sign-in; refused while the tenant has fewer than two active tenant
  admins: ``second_admin_required``, never an operator path); ``POST /v1/legal-holds/{id}/release/approve``
  by a DIFFERENT principal (``same_principal``; also a database CHECK) within 72 h releases it, with
  the cooling-off (D4: the tenant's ``hold_cooling_off_days``) during which nothing it uncovered may be
  destroyed. An expired request is recorded (``legal_hold_release_expired``) and a new one may be made.
- Read: ``GET /v1/legal-holds`` (cursor; filters ``matter_id``, ``client_id``, ``active``) and
  ``GET /v1/legal-holds/{id}`` (``legal_hold.read``).

Every placement, request, approval, expiry and release is a custody event in the tenant stream,
anchored, with an alert. Nothing is deleted: a released hold stays, a new hold is a new row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import Caller, require_recent_sign_in
from edisc_api.authz import ROLE_PERMISSIONS, TENANT, P, Scope, authorize, perm
from edisc_api.error_codes import E
from edisc_api.errors import ApiError, conflict, not_found
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, page_of
from edisc_core.ids import new_id
from edisc_custody.log import AppendedEvent
from edisc_db.session import tenant_tx
from edisc_worker.contracts import HOLDS_QUEUE, hold_workflow_id

router = APIRouter(prefix="/v1")
RELEASE_APPROVAL_WINDOW = timedelta(hours=72)  # D3


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HoldIn(Strict):
    reason: str = Field(min_length=1, max_length=2000)
    reference: str | None = Field(default=None, min_length=1, max_length=200)


class ReleaseIn(Strict):
    reason: str = Field(min_length=1, max_length=2000)


class FlagsOut(Strict):
    """The S3 legal-hold flags (D2), as the store confirmed them: ``objects_in_scope`` is null until
    a full pass over the scope finished; ``pending`` = in scope and not yet confirmed."""

    state: str
    objects_flagged: int
    objects_in_scope: int | None
    pending: int | None
    objects_cleared: int
    last_pass_completed_at: datetime | None


class ReleaseRequestOut(Strict):
    id: uuid.UUID
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    reason: str


class HoldOut(Strict):
    id: uuid.UUID
    scope: str  # matter | client
    matter_id: uuid.UUID | None
    client_id: uuid.UUID | None
    reason: str
    reference: str | None
    placed_by: str
    placed_at: datetime
    active: bool
    released_at: datetime | None
    released_by: str | None
    release_reason: str | None
    cooling_off_until: datetime | None
    flags: FlagsOut
    release_request: ReleaseRequestOut | None


HOLD_COLUMNS = (
    "h.id, h.matter_id, h.client_id, h.reason, h.reference, h.placed_by, h.placed_at,"
    " h.released_at, h.released_by, h.release_reason, h.cooling_off_until, h.flag_state,"
    " h.objects_flagged, h.objects_in_scope, h.objects_cleared, h.last_pass_completed_at,"
    " r.id AS r_id, r.requested_by AS r_by, r.requested_at AS r_at, r.expires_at AS r_exp,"
    " r.reason AS r_reason"
)
HOLD_FROM = (
    " FROM legal_holds h LEFT JOIN legal_hold_release_requests r"
    " ON r.hold_id = h.id AND r.decision IS NULL"
)


def _out(row: object) -> HoldOut:
    r = row._mapping  # type: ignore[attr-defined]
    in_scope = r["objects_in_scope"]
    return HoldOut(
        id=r["id"], scope="matter" if r["matter_id"] else "client", matter_id=r["matter_id"],
        client_id=r["client_id"], reason=r["reason"], reference=r["reference"],
        placed_by=r["placed_by"], placed_at=r["placed_at"], active=r["released_at"] is None,
        released_at=r["released_at"], released_by=r["released_by"],
        release_reason=r["release_reason"], cooling_off_until=r["cooling_off_until"],
        flags=FlagsOut(
            state=r["flag_state"], objects_flagged=r["objects_flagged"], objects_in_scope=in_scope,
            pending=None if in_scope is None else max(0, in_scope - r["objects_flagged"]),
            objects_cleared=r["objects_cleared"],
            last_pass_completed_at=r["last_pass_completed_at"],
        ),
        release_request=None if r["r_id"] is None else ReleaseRequestOut(
            id=r["r_id"], requested_by=r["r_by"], requested_at=r["r_at"], expires_at=r["r_exp"],
            reason=r["r_reason"],
        ),
    )  # fmt: skip


async def _one(s: AsyncSession, hold_id: uuid.UUID) -> HoldOut:
    row = (
        await s.execute(text(f"SELECT {HOLD_COLUMNS}{HOLD_FROM} WHERE h.id = :i"), {"i": hold_id})
    ).one()
    return _out(row)


async def _alert(s: AsyncSession, tenant_id: uuid.UUID, kind: str, message: str) -> None:
    """One alert per event: tenant admins and the matter's managers read the tenant's alerts."""
    await s.execute(
        text("INSERT INTO alerts (id, tenant_id, kind, message) VALUES (:i, :t, :k, :m)"),
        {"i": new_id(), "t": tenant_id, "k": kind, "m": message[:2000]},
    )


async def start_flagging(res: Resources, tenant_id: uuid.UUID, hold_id: uuid.UUID) -> None:
    """Set (or, after a release, clear) the hold's S3 flags in the background. One workflow per
    hold; a second start while one runs is a no-op (it re-reads the hold's state anyway)."""
    await res.temporal.start_workflow(
        "LegalHoldWorkflow",
        args=[str(tenant_id), str(hold_id)],
        id=hold_workflow_id(hold_id),
        task_queue=HOLDS_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


async def _place(res: Resources, caller: Caller, rid: str, scope: Scope, body: HoldIn) -> HoldOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.LEGAL_HOLD_PLACE, scope)
        signed_in = require_recent_sign_in(caller, res.settings)
        # lock the scope row: a destruction's per-object re-check holds it FOR SHARE (M18 §2.4)
        table = "matters" if scope.type == "matter" else "clients"
        lock = (  # one of two fixed statements, never a request value
            "SELECT 1 FROM matters WHERE id = :i FOR NO KEY UPDATE"
            if table == "matters"
            else "SELECT 1 FROM clients WHERE id = :i FOR NO KEY UPDATE"
        )
        await s.execute(text(lock), {"i": scope.id})
        hold_id = new_id()
        await s.execute(
            text(
                "INSERT INTO legal_holds (id, tenant_id, matter_id, client_id, reason, reference,"
                " placed_by, placed_by_principal) VALUES (:i, :t, :m, :c, :r, :ref, :by, :p)"
            ),
            {"i": hold_id, "t": caller.tenant_id,
             "m": scope.id if scope.type == "matter" else None,
             "c": scope.id if scope.type == "client" else None,
             "r": body.reason, "ref": body.reference, "by": caller.actor, "p": caller.principal_id},
        )  # fmt: skip
        event = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="legal_hold_placed",
            payload={"hold_id": str(hold_id), "scope": scope.type, "scope_id": str(scope.id),
                     "reason": body.reason, "reference": body.reference,
                     "authenticated_at": signed_in},
            request_id=rid,
        )  # fmt: skip
        await _alert(
            s, caller.tenant_id, "legal_hold_placed",
            f"legal hold {hold_id} placed on {scope.type} {scope.id} by {caller.actor}",
        )  # fmt: skip
        out = await _one(s, hold_id)
    await audit.anchor_change(res.sessions, res.s3, res.settings, caller.tenant_id, event)
    await start_flagging(res, caller.tenant_id, hold_id)
    return out


PLACE_CODES = (E.FORBIDDEN, E.NOT_FOUND, E.REAUTH_REQUIRED)


@router.post(
    "/matters/{matter_id}/legal-holds", status_code=201, response_model=HoldOut,
    openapi_extra=perm(P.LEGAL_HOLD_PLACE, *PLACE_CODES),
)  # fmt: skip
async def place_matter_hold(
    matter_id: uuid.UUID, body: HoldIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> HoldOut:
    """A hold on the matter: every object behind its jobs (deduplicated files included), its
    productions, reports, seals and anchors. Allowed on a closed matter (its evidence is then
    extended again)."""
    return await _place(res, caller, rid, Scope("matter", matter_id), body)


@router.post(
    "/clients/{client_id}/legal-holds", status_code=201, response_model=HoldOut,
    openapi_extra=perm(P.LEGAL_HOLD_PLACE, *PLACE_CODES),
)  # fmt: skip
async def place_client_hold(
    client_id: uuid.UUID, body: HoldIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> HoldOut:
    """A hold on the client: every matter of it, and its Slack exports."""
    return await _place(res, caller, rid, Scope("client", client_id), body)


async def _hold_scope(s: AsyncSession, hold_id: uuid.UUID) -> Scope:
    row = (
        await s.execute(
            text("SELECT matter_id, client_id FROM legal_holds WHERE id = :i"), {"i": hold_id}
        )
    ).one_or_none()
    if row is None:
        raise not_found()
    return Scope("matter", row.matter_id) if row.matter_id else Scope("client", row.client_id)


async def active_tenant_admins(s: AsyncSession) -> int:
    """Active USER principals holding tenant_admin at the tenant, directly or through a group's
    recorded members (the two-person rule needs two people)."""
    n: int = (
        await s.execute(
            text(
                "SELECT count(DISTINCT p.id) FROM principals p WHERE p.active AND p.kind = 'user'"
                " AND EXISTS (SELECT 1 FROM role_assignments r WHERE r.revoked_at IS NULL"
                "   AND r.role = 'tenant_admin' AND r.scope_type = 'tenant' AND ("
                "     r.principal_id = p.id OR r.group_id IN (SELECT gm.group_id FROM group_members gm"
                "       WHERE gm.principal_id = p.id AND gm.removed_at IS NULL)))"
            )
        )
    ).scalar_one()
    return n


async def _expire_pending(
    s: AsyncSession, caller_tenant: uuid.UUID, hold_id: uuid.UUID
) -> AppendedEvent | None:
    """Record a pending request past its expiry as expired (event + alert): its event, if one was."""
    row = (
        await s.execute(
            text(
                "UPDATE legal_hold_release_requests SET decision = 'expired', decided_at = now()"
                " WHERE hold_id = :h AND decision IS NULL AND expires_at <= now()"
                " RETURNING id, requested_by"
            ),
            {"h": hold_id},
        )
    ).one_or_none()
    if row is None:
        return None
    event = await audit.record(
        s, tenant_id=caller_tenant, actor="system:legal-hold", event_type="legal_hold_release_expired",
        payload={"hold_id": str(hold_id), "request_id": str(row.id), "requested_by": row.requested_by},
    )  # fmt: skip
    await _alert(
        s, caller_tenant, "legal_hold_release_expired",
        f"the request to release legal hold {hold_id} expired without a second approval",
    )  # fmt: skip
    return event


@router.post(
    "/legal-holds/{hold_id}/release", response_model=HoldOut,
    openapi_extra=perm(P.LEGAL_HOLD_RELEASE, E.CONFLICT, E.FORBIDDEN, E.NOT_FOUND,
                       E.REAUTH_REQUIRED, E.SECOND_ADMIN_REQUIRED),
)  # fmt: skip
async def request_hold_release(
    hold_id: uuid.UUID, body: ReleaseIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> HoldOut:
    """Ask for a release; the hold stays in force until a second, different tenant admin approves."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.LEGAL_HOLD_READ, await _hold_scope(s, hold_id))
        await authorize(s, caller, P.LEGAL_HOLD_RELEASE, TENANT)
        signed_in = require_recent_sign_in(caller, res.settings)
        hold = (await s.execute(text(
            "SELECT released_at FROM legal_holds WHERE id = :i FOR NO KEY UPDATE"), {"i": hold_id}
        )).one()  # fmt: skip
        if hold.released_at is not None:
            raise conflict("the hold is already released")
        if await active_tenant_admins(s) < 2:
            raise ApiError(
                409, "second_admin_required",
                "releasing a legal hold needs two different tenant admins; add a second one first",
            )  # fmt: skip
        await _expire_pending(s, caller.tenant_id, hold_id)
        pending = (await s.execute(text(
            "SELECT 1 FROM legal_hold_release_requests WHERE hold_id = :h AND decision IS NULL"),
            {"h": hold_id})).first()  # fmt: skip
        if pending:
            raise conflict("a release request is already waiting for approval")
        request_id = new_id()
        await s.execute(
            text(
                "INSERT INTO legal_hold_release_requests (id, tenant_id, hold_id, requested_by,"
                " requested_by_principal, reason, expires_at)"
                " VALUES (:i, :t, :h, :by, :p, :r, now() + :w)"
            ),
            {"i": request_id, "t": caller.tenant_id, "h": hold_id, "by": caller.actor,
             "p": caller.principal_id, "r": body.reason, "w": RELEASE_APPROVAL_WINDOW},
        )  # fmt: skip
        event = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor,
            event_type="legal_hold_release_requested",
            payload={"hold_id": str(hold_id), "request_id": str(request_id), "reason": body.reason,
                     "authenticated_at": signed_in},
            request_id=rid,
        )  # fmt: skip
        await _alert(
            s, caller.tenant_id, "legal_hold_release_requested",
            f"{caller.actor} asked to release legal hold {hold_id}; a second tenant admin must approve",
        )  # fmt: skip
        out = await _one(s, hold_id)
    await audit.anchor_change(res.sessions, res.s3, res.settings, caller.tenant_id, event)
    return out


@router.post(
    "/legal-holds/{hold_id}/release/approve", response_model=HoldOut,
    openapi_extra=perm(P.LEGAL_HOLD_RELEASE, E.CONFLICT, E.FORBIDDEN, E.NOT_FOUND,
                       E.REAUTH_REQUIRED, E.SAME_PRINCIPAL),
)  # fmt: skip
async def approve_hold_release(
    hold_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> HoldOut:
    """The second person: releases the hold now; the cooling-off starts; the flags are cleared in
    the background on versions no other active hold covers."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:  # an expiry is recorded, committed
        await authorize(s, caller, P.LEGAL_HOLD_READ, await _hold_scope(s, hold_id))
        await authorize(s, caller, P.LEGAL_HOLD_RELEASE, TENANT)
        expired = await _expire_pending(s, caller.tenant_id, hold_id)
    if expired is not None:
        await audit.anchor_change(res.sessions, res.s3, res.settings, caller.tenant_id, expired)
        raise conflict("the release request expired; ask again")
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        signed_in = require_recent_sign_in(caller, res.settings)
        await s.execute(
            text("SELECT 1 FROM legal_holds WHERE id = :i FOR NO KEY UPDATE"), {"i": hold_id}
        )
        req = (await s.execute(text(
            "SELECT id, requested_by, requested_by_principal, reason FROM legal_hold_release_requests"
            " WHERE hold_id = :h AND decision IS NULL AND expires_at > now()"),
            {"h": hold_id})).one_or_none()  # fmt: skip
        if req is None:
            raise conflict("no release request is waiting for approval")
        if req.requested_by_principal == caller.principal_id:
            raise ApiError(403, "same_principal", "the admin who asked cannot also approve")
        days: int = (await s.execute(text(
            "SELECT hold_cooling_off_days FROM tenants WHERE id = :t"), {"t": caller.tenant_id}
        )).scalar_one()  # fmt: skip
        await s.execute(
            text(
                "UPDATE legal_hold_release_requests SET decision = 'approved', decided_at = now(),"
                " decided_by = :by, decided_by_principal = :p WHERE id = :i"
            ),
            {"by": caller.actor, "p": caller.principal_id, "i": req.id},
        )
        await s.execute(
            text(
                "UPDATE legal_holds SET released_at = now(), released_by = :by,"
                " release_reason = :r, cooling_off_until = now() + :c, flag_state = 'clearing'"
                " WHERE id = :i AND released_at IS NULL"
            ),
            {"by": caller.actor, "r": req.reason, "c": timedelta(days=days), "i": hold_id},
        )
        event = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="legal_hold_released",
            payload={"hold_id": str(hold_id), "request_id": str(req.id),
                     "requested_by": req.requested_by, "approved_by": caller.actor,
                     "reason": req.reason, "cooling_off_days": days,
                     "authenticated_at": signed_in},
            request_id=rid,
        )  # fmt: skip
        await _alert(
            s, caller.tenant_id, "legal_hold_released",
            f"legal hold {hold_id} released by {req.requested_by} and {caller.actor};"
            f" nothing it covered may be destroyed for {days} days",
        )  # fmt: skip
        out = await _one(s, hold_id)
    await audit.anchor_change(res.sessions, res.s3, res.settings, caller.tenant_id, event)
    await start_flagging(res, caller.tenant_id, hold_id)
    return out


async def _readable_scopes(
    s: AsyncSession, caller: Caller
) -> tuple[set[uuid.UUID] | None, set[uuid.UUID]]:
    """(matter ids, client ids) where the caller holds ``legal_hold.read``; (None, ...) = the whole
    tenant. Exactly what ``authorize`` would allow for each hold (a test compares them per role)."""
    rows = (
        await s.execute(
            text(
                "SELECT role, scope_type, scope_id FROM role_assignments WHERE revoked_at IS NULL"
                " AND (principal_id = :p OR group_id = ANY(:g))"
            ),
            {"p": caller.principal_id, "g": list(caller.group_ids)},
        )
    ).all()
    grants = [r for r in rows if P.LEGAL_HOLD_READ in ROLE_PERMISSIONS.get(r.role, frozenset())]
    if any(r.scope_type == "tenant" for r in grants):
        return None, set()
    return (
        {r.scope_id for r in grants if r.scope_type == "matter"},
        {r.scope_id for r in grants if r.scope_type == "client"},
    )


@router.get(
    "/legal-holds", response_model=Page[HoldOut],
    openapi_extra=perm("visible", E.UNPROCESSABLE),
)  # fmt: skip
async def list_holds(
    caller: CallerDep,
    res: ResourcesDep,
    matter_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    active: bool | None = None,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[HoldOut]:
    """The holds the caller may read: ``legal_hold.read`` at the hold's matter, its client or the
    tenant (a join on role assignments, like ``GET /v1/jobs``)."""
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        matters, clients = await _readable_scopes(s, caller)
        rows = (
            await s.execute(
                text(
                    f"SELECT {HOLD_COLUMNS}{HOLD_FROM} LEFT JOIN matters hm ON hm.id = h.matter_id"
                    " WHERE (CAST(:after AS uuid) IS NULL OR h.id > :after)"
                    " AND (CAST(:m AS uuid) IS NULL OR h.matter_id = :m)"
                    " AND (CAST(:c AS uuid) IS NULL OR h.client_id = :c OR hm.client_id = :c)"
                    " AND (CAST(:a AS boolean) IS NULL OR (h.released_at IS NULL) = :a)"
                    " AND (CAST(:all AS boolean) OR h.matter_id = ANY(:ms) OR h.client_id = ANY(:cs)"
                    "      OR hm.client_id = ANY(:cs))"
                    " ORDER BY h.id LIMIT :n"
                ),
                {
                    "after": after,
                    "m": matter_id,
                    "c": client_id,
                    "a": active,
                    "all": matters is None,
                    "ms": list(matters or ()),
                    "cs": list(clients or ()),
                    "n": limit + 1,
                },
            )
        ).all()
    return page_of([_out(r) for r in rows], limit, lambda h: h.id)


@router.get(
    "/legal-holds/{hold_id}", response_model=HoldOut,
    openapi_extra=perm(P.LEGAL_HOLD_READ, E.FORBIDDEN, E.NOT_FOUND),
)  # fmt: skip
async def get_hold(hold_id: uuid.UUID, caller: CallerDep, res: ResourcesDep) -> HoldOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.LEGAL_HOLD_READ, await _hold_scope(s, hold_id))
        return await _one(s, hold_id)
