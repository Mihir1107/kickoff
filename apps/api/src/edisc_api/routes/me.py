"""The authenticated caller and their own sessions (no permission needed beyond being an active
principal of the tenant)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, ResourcesDep
from edisc_api.auth import Caller, require_recent_sign_in
from edisc_api.authz import ROLE_PERMISSIONS, authenticated, permissions
from edisc_api.error_codes import E
from edisc_api.errors import ApiError, not_found
from edisc_api.pagination import (
    CursorQ,
    LimitQ,
    Page,
    decode,
    decode_keys,
    decode_text,
    encode_keys,
    encode_text,
    page_of,
)
from edisc_db.session import tenant_tx

router = APIRouter(prefix="/v1")


class Me(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: uuid.UUID
    kind: str
    subject: str
    issuer: str


@router.get("/me", response_model=Me, openapi_extra=authenticated())
async def me(caller: CallerDep) -> Me:
    return Me(
        principal_id=caller.principal_id,
        kind=caller.kind,
        subject=caller.subject,
        issuer=caller.issuer,
    )


# ------------------------------------------------------------------ sessions (ADR 0016 §2)
class SessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    current: bool  # the session this request was made with
    live: bool
    created_at: datetime
    authenticated_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None
    revoke_reason: str | None
    user_agent: str | None


SESSION_SELECT = (
    "SELECT id, created_at, authenticated_at, last_seen_at, idle_expires_at, absolute_expires_at,"
    " revoked_at, revoke_reason, user_agent,"
    " (revoked_at IS NULL AND idle_expires_at > now() AND absolute_expires_at > now()) AS live"
    " FROM sessions"
)


def _session_out(row: Any, caller: Caller) -> SessionOut:
    return SessionOut(**{**row._mapping, "current": row.id == caller.session_id})


@router.get(
    "/me/sessions", response_model=Page[SessionOut], openapi_extra=authenticated(E.UNPROCESSABLE)
)
async def my_sessions(
    caller: CallerDep, res: ResourcesDep, cursor: CursorQ = None, limit: LimitQ = 50
) -> Page[SessionOut]:
    """The caller's own sessions, live and ended (nothing is deleted), oldest first."""
    after = decode(cursor)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    SESSION_SELECT + " WHERE principal_id = :p"
                    " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
                ),
                {"p": caller.principal_id, "after": after, "n": limit + 1},
            )
        ).all()
    return page_of([_session_out(r, caller) for r in rows], limit, lambda x: x.id)


@router.post(
    "/me/sessions/{session_id}/revoke",
    response_model=SessionOut,
    openapi_extra=authenticated(E.ALREADY_REVOKED, E.NOT_FOUND, E.REAUTH_REQUIRED),
)
async def revoke_my_session(
    session_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> SessionOut:
    """Ends one of the caller's own sessions (another principal's: 404). Ending the CURRENT session
    always works; ending another one needs a recent sign-in (ADR 0016 §4)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        owned = (
            await s.execute(
                text("SELECT revoked_at FROM sessions WHERE id = :i AND principal_id = :p"),
                {"i": session_id, "p": caller.principal_id},
            )
        ).one_or_none()
        if owned is None:
            raise not_found()
        signed_in = (
            None
            if session_id == caller.session_id
            else require_recent_sign_in(caller, res.settings)
        )
        if owned.revoked_at is not None:
            raise ApiError(409, "already_revoked", "the session has already ended")
        await s.execute(
            text(
                "UPDATE sessions SET revoked_at = now(), revoked_by = :by, revoke_reason = 'self'"
                " WHERE id = :i AND revoked_at IS NULL"
            ),
            {"i": session_id, "by": caller.actor},
        )
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="session_revoked",
            payload={"session_id": str(session_id), "reason": "self",
                     "authenticated_at": signed_in},
            request_id=rid,
        )  # fmt: skip
        row = (await s.execute(text(SESSION_SELECT + " WHERE id = :i"), {"i": session_id})).one()
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return _session_out(row, caller)


# ------------------------------------------------------------------ permissions (M17 plan §3.1)
class ScopePermissionsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: str  # tenant | client | matter | workspace
    scope_id: uuid.UUID | None  # None for the tenant
    roles: list[str]  # held at this scope, directly or through a group
    permissions: list[str]  # what those roles grant at this scope and everything below it


NIL = uuid.UUID(int=0)  # the tenant scope's place in the keyset (it has no id)


@router.get(
    "/me/permissions",
    response_model=Page[ScopePermissionsOut],
    openapi_extra=authenticated(E.UNPROCESSABLE),
)
async def my_permissions(
    caller: CallerDep, res: ResourcesDep, cursor: CursorQ = None, limit: LimitQ = 50
) -> Page[ScopePermissionsOut]:
    """Every scope where the caller holds a role, directly or through a group, with the permissions
    those roles grant there (`authz.permissions`, the code `authorize` uses). A hint for showing
    controls: what is inherited below a scope is implied, and every action is still authorized by
    the API. Keyset on (scope type, scope id): a principal may hold thousands of matter roles."""
    after = decode_keys(cursor, 2)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT scope_type, scope_id, array_agg(DISTINCT role) AS roles"
                    " FROM role_assignments WHERE revoked_at IS NULL"
                    " AND (principal_id = :p OR group_id = ANY(:g))"
                    " GROUP BY scope_type, scope_id"
                    " HAVING CAST(:at AS text) IS NULL"
                    ' OR (scope_type COLLATE "C", COALESCE(scope_id, :nil))'
                    ' > (CAST(:at AS text) COLLATE "C", CAST(:ai AS uuid))'
                    ' ORDER BY scope_type COLLATE "C", scope_id NULLS FIRST LIMIT :n'
                ),
                {"p": caller.principal_id, "g": list(caller.group_ids), "nil": NIL,
                 "at": after[0] if after else None, "ai": after[1] if after else None,
                 "n": limit + 1},
            )
        ).all()  # fmt: skip
    items = [
        ScopePermissionsOut(
            scope_type=r.scope_type,
            scope_id=r.scope_id,
            roles=sorted(r.roles),
            permissions=sorted(permissions(set(r.roles))),
        )
        for r in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = encode_keys(last.scope_type, str(last.scope_id or NIL))
    return Page[ScopePermissionsOut](items=items, next_cursor=next_cursor)


class RoleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    permissions: list[str]


@router.get("/roles", response_model=Page[RoleOut], openapi_extra=authenticated(E.UNPROCESSABLE))
async def roles(caller: CallerDep, cursor: CursorQ = None, limit: LimitQ = 50) -> Page[RoleOut]:
    """The fixed roles and what each grants, generated from `authz.ROLE_PERMISSIONS` (nothing else
    defines a role)."""
    after = decode_text(cursor)
    names = sorted(r for r in ROLE_PERMISSIONS if after is None or r > after)
    items = [RoleOut(role=r, permissions=sorted(ROLE_PERMISSIONS[r])) for r in names[:limit]]
    more = len(names) > limit
    return Page[RoleOut](items=items, next_cursor=encode_text(items[-1].role) if more else None)
