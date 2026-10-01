"""Tenant administration: principals, groups, role assignments (``tenant.admin``). Nothing is deleted:
memberships and assignments are ended, and every change is an audit event."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, ResourcesDep
from edisc_api.authz import ROLES, TENANT, P, Scope, ancestors, authorize, perm
from edisc_api.errors import conflict, not_found, unprocessable
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, page_of
from edisc_core.ids import new_id
from edisc_db.session import tenant_tx

router = APIRouter(prefix="/v1")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrincipalIn(Strict):
    kind: Literal["user", "service"] = "user"
    issuer: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class PrincipalOut(Strict):
    id: uuid.UUID
    kind: str
    issuer: str
    subject: str
    display_name: str
    email: str | None
    active: bool
    created_at: datetime


class GroupIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    external_id: str | None = Field(default=None, max_length=500)


class GroupOut(Strict):
    id: uuid.UUID
    name: str
    external_id: str | None
    created_at: datetime


class MemberIn(Strict):
    principal_id: uuid.UUID


class AssignmentIn(Strict):
    principal_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    role: str
    scope_type: Literal["tenant", "client", "matter", "workspace"]
    scope_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> AssignmentIn:
        if (self.principal_id is None) == (self.group_id is None):
            raise ValueError("exactly one of principal_id and group_id")
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role}")
        if (self.scope_type == "tenant") != (self.scope_id is None):
            raise ValueError("scope_id is required except for scope_type tenant")
        return self


class AssignmentOut(Strict):
    id: uuid.UUID
    principal_id: uuid.UUID | None
    group_id: uuid.UUID | None
    role: str
    scope_type: str
    scope_id: uuid.UUID | None
    created_at: datetime
    created_by: str
    revoked_at: datetime | None
    revoked_by: str | None


ADMIN = perm(P.TENANT_ADMIN)


@router.post("/principals", status_code=201, response_model=PrincipalOut, openapi_extra=ADMIN)
async def create_principal(
    body: PrincipalIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> PrincipalOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        try:
            async with s.begin_nested():
                row = (
                    await s.execute(
                        text(
                            "INSERT INTO principals (id, tenant_id, kind, issuer, subject, display_name, email)"
                            " VALUES (:i, :t, :k, :iss, :sub, :n, :e)"
                            " RETURNING id, kind, issuer, subject, display_name, email, active, created_at"
                        ),
                        {"i": new_id(), "t": caller.tenant_id, "k": body.kind, "iss": body.issuer,
                         "sub": body.subject, "n": body.display_name, "e": body.email},
                    )
                ).one()  # fmt: skip
        except IntegrityError as exc:
            raise conflict("a principal with this issuer and subject exists") from exc
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="principal_created",
            payload={"principal_id": str(row.id), "kind": body.kind, "issuer": body.issuer,
                     "subject": body.subject},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return PrincipalOut(**row._mapping)


@router.post(
    "/principals/{principal_id}/deactivate", response_model=PrincipalOut, openapi_extra=ADMIN
)
async def deactivate_principal(
    principal_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> PrincipalOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        row = (
            await s.execute(
                text(
                    "UPDATE principals SET active = false WHERE id = :i"
                    " RETURNING id, kind, issuer, subject, display_name, email, active, created_at"
                ),
                {"i": principal_id},
            )
        ).one_or_none()
        if row is None:
            raise not_found()
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="principal_deactivated",
            payload={"principal_id": str(principal_id)}, request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return PrincipalOut(**row._mapping)


@router.get("/principals", response_model=Page[PrincipalOut], openapi_extra=ADMIN)
async def list_principals(
    caller: CallerDep, res: ResourcesDep, cursor: CursorQ = None, limit: LimitQ = 50
) -> Page[PrincipalOut]:
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        rows = (
            await s.execute(
                text(
                    "SELECT id, kind, issuer, subject, display_name, email, active, created_at FROM principals"
                    " WHERE (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
                ),
                {"after": after, "n": limit + 1},
            )
        ).all()
    return page_of([PrincipalOut(**r._mapping) for r in rows], limit, lambda p: p.id)


@router.post("/groups", status_code=201, response_model=GroupOut, openapi_extra=ADMIN)
async def create_group(
    body: GroupIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> GroupOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        try:
            async with s.begin_nested():
                row = (
                    await s.execute(
                        text(
                            "INSERT INTO groups (id, tenant_id, name, external_id) VALUES (:i, :t, :n, :e)"
                            " RETURNING id, name, external_id, created_at"
                        ),
                        {
                            "i": new_id(),
                            "t": caller.tenant_id,
                            "n": body.name,
                            "e": body.external_id,
                        },
                    )
                ).one()
        except IntegrityError as exc:
            raise conflict("a group with this name or external id exists") from exc
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="group_created",
            payload={"group_id": str(row.id), "name": body.name, "external_id": body.external_id},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return GroupOut(**row._mapping)


@router.post("/groups/{group_id}/members", status_code=204, openapi_extra=ADMIN)
async def add_member(
    group_id: uuid.UUID, body: MemberIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> None:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        try:
            async with s.begin_nested():
                await s.execute(
                    text(
                        "INSERT INTO group_members (id, tenant_id, group_id, principal_id) VALUES (:i, :t, :g, :p)"
                    ),
                    {"i": new_id(), "t": caller.tenant_id, "g": group_id, "p": body.principal_id},
                )
        except IntegrityError as exc:
            raise conflict("unknown group or principal, or already a member") from exc
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="group_member_added",
            payload={"group_id": str(group_id), "principal_id": str(body.principal_id)}, request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)


@router.post(
    "/groups/{group_id}/members/{principal_id}/remove", status_code=204, openapi_extra=ADMIN
)
async def remove_member(
    group_id: uuid.UUID,
    principal_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
) -> None:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        ended = (
            await s.execute(
                text(
                    "UPDATE group_members SET removed_at = now() WHERE group_id = :g AND principal_id = :p"
                    " AND removed_at IS NULL RETURNING id"
                ),
                {"g": group_id, "p": principal_id},
            )
        ).first()
        if ended is None:
            raise not_found()
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="group_member_removed",
            payload={"group_id": str(group_id), "principal_id": str(principal_id)}, request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)


@router.post(
    "/role-assignments", status_code=201, response_model=AssignmentOut, openapi_extra=ADMIN
)
async def assign_role(
    body: AssignmentIn, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> AssignmentOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        if (
            body.scope_type != "tenant"
            and await ancestors(s, Scope(body.scope_type, body.scope_id)) is None
        ):
            raise unprocessable(f"unknown {body.scope_type}")
        try:
            async with s.begin_nested():
                row = (
                    await s.execute(
                        text(
                            "INSERT INTO role_assignments (id, tenant_id, principal_id, group_id, role, scope_type,"
                            " scope_id, created_by) VALUES (:i, :t, :p, :g, :r, :st, :sid, :by)"
                            " RETURNING id, principal_id, group_id, role, scope_type, scope_id, created_at,"
                            " created_by, revoked_at, revoked_by"
                        ),
                        {"i": new_id(), "t": caller.tenant_id, "p": body.principal_id, "g": body.group_id,
                         "r": body.role, "st": body.scope_type, "sid": body.scope_id, "by": caller.actor},
                    )
                ).one()  # fmt: skip
        except IntegrityError as exc:
            raise unprocessable("unknown principal or group") from exc
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="role_assigned",
            payload={"assignment_id": str(row.id), "principal_id": str(body.principal_id) if body.principal_id else None,
                     "group_id": str(body.group_id) if body.group_id else None, "role": body.role,
                     "scope_type": body.scope_type, "scope_id": str(body.scope_id) if body.scope_id else None},
            request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return AssignmentOut(**row._mapping)


@router.post(
    "/role-assignments/{assignment_id}/revoke", response_model=AssignmentOut, openapi_extra=ADMIN
)
async def revoke_role(
    assignment_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> AssignmentOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        row = (
            await s.execute(
                text(
                    "UPDATE role_assignments SET revoked_at = now(), revoked_by = :by"
                    " WHERE id = :i AND revoked_at IS NULL"
                    " RETURNING id, principal_id, group_id, role, scope_type, scope_id, created_at,"
                    " created_by, revoked_at, revoked_by"
                ),
                {"i": assignment_id, "by": caller.actor},
            )
        ).one_or_none()
        if row is None:
            raise not_found()
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="role_revoked",
            payload={"assignment_id": str(assignment_id)}, request_id=rid,
        )  # fmt: skip
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return AssignmentOut(**row._mapping)


@router.get("/role-assignments", response_model=Page[AssignmentOut], openapi_extra=ADMIN)
async def list_assignments(
    caller: CallerDep, res: ResourcesDep, cursor: CursorQ = None, limit: LimitQ = 50
) -> Page[AssignmentOut]:
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.TENANT_ADMIN, TENANT)
        rows = (
            await s.execute(
                text(
                    "SELECT id, principal_id, group_id, role, scope_type, scope_id, created_at, created_by,"
                    " revoked_at, revoked_by FROM role_assignments"
                    " WHERE (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
                ),
                {"after": after, "n": limit + 1},
            )
        ).all()
    return page_of([AssignmentOut(**r._mapping) for r in rows], limit, lambda a: a.id)
