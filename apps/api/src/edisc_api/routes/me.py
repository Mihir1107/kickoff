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
from edisc_api.errors import ApiError, not_found
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, page_of
from edisc_db.session import tenant_tx

router = APIRouter(prefix="/v1")
AUTHENTICATED = {"x-permission": "authenticated"}


class Me(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: uuid.UUID
    kind: str
    subject: str
    issuer: str


@router.get("/me", response_model=Me, openapi_extra=AUTHENTICATED)
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


@router.get("/me/sessions", response_model=Page[SessionOut], openapi_extra=AUTHENTICATED)
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
    "/me/sessions/{session_id}/revoke", response_model=SessionOut, openapi_extra=AUTHENTICATED
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
