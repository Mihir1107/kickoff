"""A connection's directory for the scope picker (M17 plan §3.3; D12, D14; migration 0043).

``GET /v1/connections/{id}/directory?kind=conversation|user&q=&cursor=&limit=`` serves the
connection's current COMPLETE generation (``edisc_worker.directory`` captures them in the
background): it never calls the source. A generation still capturing is never served; "not
captured yet" is its own answer (``status`` ``not_captured``, ``items`` null), never an empty list.

- **Who** (D12, the plan's default pending the mentor): ``connection.read`` on the connection's
  client, or ``job.start`` on the client or on one of its matters (the picker of a job). Names of a
  workspace's users are personal data: reviewers and auditors do not see them. Anyone else gets
  what ``connection.read`` would answer (404 when the client is invisible, 403 when visible).
- **Pages** (D14): at most 500 entries, keyset on ``(search_key, external_id)`` (code-point order,
  the search index), ``q`` a prefix of the normalised name (``search_key``), as a range on the
  index. The cursor names its generation, so paging continues in the generation it started in even
  after a newer one completes (complete and superseded generations stay readable).
- Every page states its ``snapshot_id``, ``generation``, ``captured_at``, ``refresh_running`` and
  ``approximate_total`` (the generation's count of that kind).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edisc_api.app import CallerDep, ResourcesDep
from edisc_api.auth import Caller
from edisc_api.authz import (
    ROLE_PERMISSIONS,
    P,
    Scope,
    ancestors,
    authorize,
    perm,
    permissions,
    roles_at,
)
from edisc_api.error_codes import E
from edisc_api.errors import not_found, unprocessable
from edisc_api.pagination import CursorQ, decode_keys, encode_keys
from edisc_db.session import tenant_tx
from edisc_normalizer.directory import search_key

router = APIRouter(prefix="/v1")
MAX_PAGE = 500  # D14
PERMISSION = "connection.read|job.start"  # either one (D12)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectoryKind(StrEnum):
    CONVERSATION = "conversation"
    USER = "user"


class DirectoryEntryOut(Strict):
    external_id: str
    name: str | None
    # the exact UTF-16 code units of a name that held a lone surrogate (``name`` is its replacement)
    name_code_units: list[int] | None
    entry_type: str  # public_channel, private_channel, im, mpim / user, bot
    archived: bool  # deactivated, for users


class DirectoryOut(Strict):
    status: Literal["captured", "not_captured"]
    kind: DirectoryKind
    snapshot_id: uuid.UUID | None
    generation: int | None
    captured_at: datetime | None
    refresh_running: bool
    approximate_total: int | None
    items: list[DirectoryEntryOut] | None  # None: not captured yet (never an empty list for that)
    next_cursor: str | None


DirLimitQ = Annotated[int, Query(ge=1, le=MAX_PAGE)]
SearchQ = Annotated[str | None, Query(min_length=1, max_length=200)]

GENERATION = (
    "SELECT id, generation, completed_at, conversations, users FROM directory_snapshots"
    " WHERE connection_id = :c AND status = 'complete' ORDER BY generation DESC LIMIT 1"
)
GENERATION_BY_ID = (
    "SELECT id, generation, completed_at, conversations, users FROM directory_snapshots"
    " WHERE id = :s AND connection_id = :c AND status IN ('complete', 'superseded')"
)
RUNNING = (
    "SELECT EXISTS (SELECT 1 FROM directory_snapshots WHERE connection_id = :c"
    " AND status = 'capturing')"
)
PAGE = (
    "SELECT external_id, name, name_units, entry_type, archived, search_key FROM directory_entries"
    " WHERE snapshot_id = :s AND kind = :k"
    " AND (CAST(:lo AS text) IS NULL OR search_key >= CAST(:lo AS text))"
    " AND (CAST(:hi AS text) IS NULL OR search_key < CAST(:hi AS text))"
    " AND (CAST(:after_key AS text) IS NULL"
    "  OR (search_key, external_id) > (CAST(:after_key AS text), CAST(:after_id AS text)))"
    ' ORDER BY search_key COLLATE "C", external_id COLLATE "C" LIMIT :n'
)


def prefix_bounds(q: str | None) -> tuple[str | None, str | None]:
    """``[lo, hi)`` holding exactly the keys that start with the normalised ``q`` under code-point
    order (the columns are COLLATE "C"): ``hi`` is the shortest string greater than every such
    key. Surrogate code points are skipped (the database stores none)."""
    if q is None:
        return None, None
    lo = search_key(q)
    if not lo:
        return None, None
    chars = list(lo)
    while chars:
        cp = ord(chars[-1]) + 1
        if 0xD800 <= cp <= 0xDFFF:
            cp = 0xE000
        if cp <= 0x10FFFF:
            chars[-1] = chr(cp)
            return lo, "".join(chars)
        chars.pop()
    return lo, None  # every character is U+10FFFF: no upper bound


async def _may_pick(s: AsyncSession, caller: Caller, client_id: uuid.UUID) -> bool:
    """``connection.read`` or ``job.start`` at the client (or the tenant), or ``job.start`` on a
    matter of the client (the scope picker of a job)."""
    chain = await ancestors(s, Scope("client", client_id))
    if chain is None:
        return False
    granted = permissions(await roles_at(s, caller, chain))
    if P.CONNECTION_READ in granted or P.JOB_START in granted:
        return True
    starters = sorted(r for r, perms in ROLE_PERMISSIONS.items() if P.JOB_START in perms)
    found: bool = (
        await s.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM role_assignments a JOIN matters m"
                " ON a.scope_type = 'matter' AND m.id = a.scope_id WHERE m.client_id = :c"
                " AND a.revoked_at IS NULL AND a.role = ANY(:roles)"
                " AND (a.principal_id = :p OR a.group_id = ANY(:g)))"
            ),
            {
                "c": client_id,
                "roles": starters,
                "p": caller.principal_id,
                "g": list(caller.group_ids),
            },
        )
    ).scalar_one()
    return bool(found)


@router.get(
    "/connections/{connection_id}/directory",
    response_model=DirectoryOut,
    openapi_extra=perm(PERMISSION, E.FORBIDDEN, E.NOT_FOUND),
)
async def get_directory(
    connection_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    kind: DirectoryKind,
    q: SearchQ = None,
    cursor: CursorQ = None,
    limit: DirLimitQ = 200,
) -> DirectoryOut:
    """A page of the connection's directory (its conversations or its users) from its current
    complete snapshot; never a call to the source. See the module docstring."""
    position = decode_keys(cursor, 3)
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        client: uuid.UUID | None = (
            await s.execute(
                text("SELECT client_id FROM connections WHERE id = :i"), {"i": connection_id}
            )
        ).scalar_one_or_none()
        if client is None:
            raise not_found()
        if not await _may_pick(s, caller, client):
            await authorize(s, caller, P.CONNECTION_READ, Scope("client", client))  # 404 / 403
            raise not_found()  # unreachable: authorize grants only what _may_pick grants
        running = bool((await s.execute(text(RUNNING), {"c": connection_id})).scalar_one())
        if position is None:
            snap = (await s.execute(text(GENERATION), {"c": connection_id})).one_or_none()
        else:
            try:
                snapshot_id = uuid.UUID(position[0])
            except ValueError as exc:
                raise unprocessable("invalid cursor") from exc
            snap = (
                await s.execute(text(GENERATION_BY_ID), {"s": snapshot_id, "c": connection_id})
            ).one_or_none()
            if snap is None:  # not this connection's (or this tenant's) generation
                raise unprocessable("invalid cursor")
        if snap is None:
            return DirectoryOut(
                status="not_captured",
                kind=kind,
                snapshot_id=None,
                generation=None,
                captured_at=None,
                refresh_running=running,
                approximate_total=None,
                items=None,
                next_cursor=None,
            )
        lo, hi = prefix_bounds(q)
        rows = (
            await s.execute(
                text(PAGE),
                {
                    "s": snap.id,
                    "k": kind.value,
                    "lo": lo,
                    "hi": hi,
                    "after_key": position[1] if position else None,
                    "after_id": position[2] if position else None,
                    "n": limit + 1,
                },
            )
        ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return DirectoryOut(
        status="captured", kind=kind, snapshot_id=snap.id, generation=snap.generation,
        captured_at=snap.completed_at, refresh_running=running,
        approximate_total=snap.conversations if kind is DirectoryKind.CONVERSATION else snap.users,
        items=[
            DirectoryEntryOut(external_id=r.external_id, name=r.name, name_code_units=r.name_units,
                              entry_type=r.entry_type, archived=r.archived)
            for r in rows
        ],
        next_cursor=encode_keys(str(snap.id), rows[-1].search_key, rows[-1].external_id)
        if more else None,
    )  # fmt: skip
