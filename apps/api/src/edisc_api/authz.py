"""Authorization: fixed roles over tenant > client > matter > workspace (ADR 0013 section 3).

- Roles are fixed permission sets defined here (versioned with the code). Tenants assign roles.
- An assignment holds for its scope and everything below it.
- Every route declares ``require(permission, scope)``. A test enumerates routes and fails on any route
  without a declared permission (``openapi_extra["x-permission"]``).
- Objects outside the caller's tenant do not exist (RLS). Inside the tenant, a caller who cannot even
  read the target gets 404 (no existence leak); one who can read it but lacks the permission gets 403.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edisc_api.auth import Caller
from edisc_api.errors import forbidden, not_found


class Permission(StrEnum):
    TENANT_ADMIN = "tenant.admin"  # IdPs, principals, groups, role assignments
    CLIENT_CREATE = "client.create"
    CLIENT_READ = "client.read"
    CONNECTION_MANAGE = (
        "connection.manage"  # create, re-authorize, disable (client-owned, decision a)
    )
    CONNECTION_READ = "connection.read"
    MATTER_CREATE = "matter.create"
    MATTER_READ = "matter.read"
    WORKSPACE_CREATE = "workspace.create"
    WORKSPACE_READ = "workspace.read"
    JOB_START = "job.start"  # also allows using the client's connections for this matter
    JOB_CANCEL = "job.cancel"
    JOB_RESUME = "job.resume"
    JOB_RERUN = "job.rerun"
    JOB_READ = "job.read"
    CUSTODY_READ = "custody.read"
    EVIDENCE_READ = "evidence.read"  # returns evidence content: always audited (decision d)


P = Permission
_READ_MATTER = {P.MATTER_READ, P.WORKSPACE_READ, P.JOB_READ, P.CLIENT_READ}
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "tenant_admin": frozenset(Permission),
    "client_admin": frozenset(
        {
            P.CLIENT_READ,
            P.CONNECTION_MANAGE,
            P.CONNECTION_READ,
            P.MATTER_CREATE,
            P.WORKSPACE_CREATE,
            P.JOB_START,
            P.JOB_CANCEL,
            P.JOB_RESUME,
            P.JOB_RERUN,
            P.CUSTODY_READ,
            P.EVIDENCE_READ,
            *_READ_MATTER,
        }
    ),
    "matter_manager": frozenset(
        {
            P.WORKSPACE_CREATE,
            P.JOB_START,
            P.JOB_CANCEL,
            P.JOB_RESUME,
            P.JOB_RERUN,
            P.CUSTODY_READ,
            P.EVIDENCE_READ,
            *_READ_MATTER,
        }
    ),
    "collector": frozenset({P.JOB_START, P.JOB_CANCEL, *_READ_MATTER}),
    "reviewer": frozenset({P.EVIDENCE_READ, *_READ_MATTER}),
    "auditor": frozenset({P.CUSTODY_READ, *_READ_MATTER}),
}
ROLES = frozenset(ROLE_PERMISSIONS)

# a caller who holds this at the target may learn that it exists (403 instead of 404)
VISIBILITY = {
    "tenant": P.CLIENT_READ,
    "client": P.CLIENT_READ,
    "matter": P.MATTER_READ,
    "workspace": P.WORKSPACE_READ,
}


@dataclass(frozen=True)
class Scope:
    type: str  # tenant | client | matter | workspace
    id: uuid.UUID | None  # None for tenant


TENANT = Scope("tenant", None)


async def ancestors(session: AsyncSession, scope: Scope) -> list[Scope] | None:
    """The scope and every scope above it, or None if the target does not exist (in this tenant)."""
    if scope.type == "tenant":
        return [TENANT]
    if scope.type == "client":
        row = (
            await session.execute(text("SELECT id FROM clients WHERE id = :i"), {"i": scope.id})
        ).one_or_none()
        return None if row is None else [scope, TENANT]
    if scope.type == "matter":
        row = (
            await session.execute(
                text("SELECT client_id FROM matters WHERE id = :i"), {"i": scope.id}
            )
        ).one_or_none()
        return None if row is None else [scope, Scope("client", row.client_id), TENANT]
    if scope.type == "workspace":
        row = (
            await session.execute(
                text(
                    "SELECT w.matter_id, m.client_id FROM workspaces w JOIN matters m ON m.id = w.matter_id"
                    " WHERE w.id = :i"
                ),
                {"i": scope.id},
            )
        ).one_or_none()
        if row is None:
            return None
        return [scope, Scope("matter", row.matter_id), Scope("client", row.client_id), TENANT]
    raise ValueError(f"unknown scope type {scope.type}")


async def roles_at(session: AsyncSession, caller: Caller, chain: list[Scope]) -> set[str]:
    """Roles the caller holds (directly or through a group) at any scope in ``chain``."""
    rows = (
        await session.execute(
            text(
                "SELECT role, scope_type, scope_id FROM role_assignments WHERE revoked_at IS NULL"
                " AND (principal_id = :p OR group_id = ANY(:g))"
            ),
            {"p": caller.principal_id, "g": list(caller.group_ids)},
        )
    ).all()
    wanted = {(s.type, s.id) for s in chain}
    return {r.role for r in rows if (r.scope_type, r.scope_id) in wanted}


def permissions(roles: set[str]) -> frozenset[Permission]:
    out: set[Permission] = set()
    for role in roles:
        out |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(out)


async def authorize(
    session: AsyncSession, caller: Caller, permission: Permission, scope: Scope
) -> None:
    """Raise 404 if the target does not exist or is invisible to the caller, 403 if visible but the
    permission is missing. Runs inside the caller's tenant transaction."""
    chain = await ancestors(session, scope)
    if chain is None:
        raise not_found()
    granted = permissions(await roles_at(session, caller, chain))
    if permission in granted:
        return
    if VISIBILITY[scope.type] in granted:
        raise forbidden(f"missing permission {permission.value}")
    raise not_found()


async def visible_ids(
    session: AsyncSession, caller: Caller, scope_type: str
) -> set[uuid.UUID] | None:
    """Ids of ``scope_type`` objects the caller can see (any role at, above or below them), or None
    meaning "all" (a tenant-level role)."""
    rows = (
        await session.execute(
            text(
                "SELECT role, scope_type, scope_id FROM role_assignments WHERE revoked_at IS NULL"
                " AND (principal_id = :p OR group_id = ANY(:g))"
            ),
            {"p": caller.principal_id, "g": list(caller.group_ids)},
        )
    ).all()
    if any(r.scope_type == "tenant" for r in rows):
        return None
    ids: set[uuid.UUID] = set()
    for r in rows:
        if r.scope_type == scope_type:
            ids.add(r.scope_id)
        elif scope_type == "client":  # a role below a client makes the client visible
            parent = await _client_of(session, r.scope_type, r.scope_id)
            if parent is not None:
                ids.add(parent)
        elif scope_type == "matter" and r.scope_type == "client":
            ids |= set(
                (
                    await session.execute(
                        text("SELECT id FROM matters WHERE client_id = :c"), {"c": r.scope_id}
                    )
                ).scalars()
            )
        elif scope_type == "matter" and r.scope_type == "workspace":
            m: uuid.UUID | None = (
                await session.execute(
                    text("SELECT matter_id FROM workspaces WHERE id = :w"), {"w": r.scope_id}
                )
            ).scalar_one_or_none()
            if m is not None:
                ids.add(m)
    return ids


async def _client_of(
    session: AsyncSession, scope_type: str, scope_id: uuid.UUID
) -> uuid.UUID | None:
    chain = await ancestors(session, Scope(scope_type, scope_id))
    if chain is None:
        return None
    return next((s.id for s in chain if s.type == "client"), None)


def perm(permission: Permission | str) -> dict[str, str]:
    """``openapi_extra`` marker: the permission a route enforces (checked by the route-table test)."""
    return {"x-permission": str(permission)}
