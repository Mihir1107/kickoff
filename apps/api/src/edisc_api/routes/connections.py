"""Source connections, owned by a client (ADR 0013 decision a).

- Only ``connection.manage`` (client_admin, tenant_admin) creates, re-authorizes or disables them.
- Credentials go straight into ``edisc_db.connection_tokens`` (envelope encryption, ADR 0009). They are
  never echoed, logged, audited or passed to Temporal: responses carry status, scopes and blind spots.
- Re-authorization resumes every job paused for re-auth on the connection (custody ``job_resumed``
  naming the acting user) and wakes their workflows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError, RPCStatusCode

from edisc_api import audit
from edisc_api.app import CallerDep, RequestIdDep, Resources, ResourcesDep
from edisc_api.auth import Caller
from edisc_api.authz import P, Permission, Scope, authorize, perm
from edisc_api.errors import not_found, unprocessable
from edisc_api.pagination import CursorQ, LimitQ, Page, decode, page_of
from edisc_connectors_base.types import Connection
from edisc_core.ids import new_id
from edisc_core.redaction import register_secret
from edisc_core.time import UtcDatetime
from edisc_db.connection_tokens import TokenSet, store_tokens
from edisc_db.session import tenant_tx
from edisc_worker.pipeline import Pipeline

router = APIRouter(prefix="/v1")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Strict):
    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr | None = None
    expires_at: UtcDatetime | None = None


class ConnectionIn(Strict):
    source: str = Field(min_length=1, max_length=50)
    external_org_id: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)  # non-secret connector settings
    credentials: Credentials


class ReauthIn(Strict):
    credentials: Credentials


class ConnectionOut(Strict):
    id: uuid.UUID
    client_id: uuid.UUID
    source: str
    external_org_id: str
    status: str
    plan_tier: str | None
    granted_scopes: list[str]
    created_at: datetime
    updated_at: datetime


SELECT_ONE = (
    "SELECT id, client_id, source, external_org_id, status, plan_tier, granted_scopes, created_at, updated_at"
    " FROM connections WHERE id = :i"
)
SELECT_PAGE = (
    "SELECT id, client_id, source, external_org_id, status, plan_tier, granted_scopes, created_at, updated_at"
    " FROM connections WHERE client_id = :c"
    " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
)


def _tokens(c: Credentials, version: int = 0) -> TokenSet:
    for secret in (c.access_token, c.refresh_token):
        if secret is not None:
            register_secret(secret)  # scrubbed from every log line of this process
    return TokenSet(c.access_token, c.refresh_token, c.expires_at, version)


async def _connection_client(s: AsyncSession, connection_id: uuid.UUID) -> uuid.UUID:
    client: uuid.UUID | None = (
        await s.execute(
            text("SELECT client_id FROM connections WHERE id = :i"), {"i": connection_id}
        )
    ).scalar_one_or_none()
    if client is None:
        raise not_found()
    return client


async def _authorize_connection(
    s: AsyncSession, caller: Caller, permission: Permission, connection_id: uuid.UUID
) -> None:
    await authorize(
        s, caller, permission, Scope("client", await _connection_client(s, connection_id))
    )


async def _out(s: AsyncSession, connection_id: uuid.UUID) -> ConnectionOut:
    row = (await s.execute(text(SELECT_ONE), {"i": connection_id})).one()
    return ConnectionOut(**row._mapping)


@router.post(
    "/clients/{client_id}/connections", status_code=201, response_model=ConnectionOut,
    openapi_extra=perm(P.CONNECTION_MANAGE),
)  # fmt: skip
async def create_connection(
    client_id: uuid.UUID,
    body: ConnectionIn,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
) -> ConnectionOut:
    connector = res.connectors.get(body.source)
    if connector is None:
        raise unprocessable(f"unknown source {body.source}")
    if connector.archive_backed:  # its connection is created by validating an upload (ADR 0014)
        raise unprocessable(f"{body.source} connections come from uploads: POST .../exports")
    connection_id = new_id()
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CONNECTION_MANAGE, Scope("client", client_id))
    tokens = _tokens(body.credentials)
    info = await connector.validate_connection(
        Connection(caller.tenant_id, connection_id, body.source, body.external_org_id, body.config)
    )
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO connections (id, tenant_id, client_id, source, external_org_id, plan_tier,"
                " granted_scopes, status, config) VALUES (:i, :t, :c, :src, :org, :tier, :scopes, 'pending',"
                " CAST(:cfg AS jsonb))"
            ),
            {"i": connection_id, "t": caller.tenant_id, "c": client_id, "src": body.source,
             "org": body.external_org_id, "tier": info.plan_tier, "scopes": list(info.granted_scopes),
             "cfg": _json(body.config)},
        )  # fmt: skip
    await store_tokens(
        res.sessions, res.box, tenant_id=caller.tenant_id, connection_id=connection_id,
        tokens=tokens, expected_version=0,
    )  # fmt: skip
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await s.execute(
            text("UPDATE connections SET status = 'active', updated_at = now() WHERE id = :i"),
            {"i": connection_id},
        )
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="connection_created",
            payload={"connection_id": str(connection_id), "client_id": str(client_id),
                     "source": body.source, "external_org_id": body.external_org_id,
                     "granted_scopes": list(info.granted_scopes), "blind_spots": list(info.blind_spots)},
            request_id=rid,
        )  # fmt: skip
        out = await _out(s, connection_id)
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return out


@router.post(
    "/connections/{connection_id}/reauth", response_model=ConnectionOut,
    openapi_extra=perm(P.CONNECTION_MANAGE),
)  # fmt: skip
async def reauthorize(
    connection_id: uuid.UUID,
    body: ReauthIn,
    caller: CallerDep,
    res: ResourcesDep,
    rid: RequestIdDep,
) -> ConnectionOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_connection(s, caller, P.CONNECTION_MANAGE, connection_id)
        version: int = (
            await s.execute(
                text("SELECT token_version FROM connections WHERE id = :i"), {"i": connection_id}
            )
        ).scalar_one()
    await store_tokens(
        res.sessions, res.box, tenant_id=caller.tenant_id, connection_id=connection_id,
        tokens=_tokens(body.credentials, version), expected_version=version,
    )  # fmt: skip
    resumed = await _pipeline(res).resume_connection(
        tenant_id=caller.tenant_id, connection_id=connection_id, actor=caller.actor
    )
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="connection_reauthorized",
            payload={"connection_id": str(connection_id), "resumed_jobs": [str(j) for j in resumed]},
            request_id=rid,
        )  # fmt: skip
        out = await _out(s, connection_id)
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    for job_id in resumed:  # fast path; the workflows also re-check the DB on their own
        try:
            await res.temporal.get_workflow_handle(str(job_id)).signal("wake")
        except RPCError as exc:
            if exc.status is not RPCStatusCode.NOT_FOUND:
                raise
    return out


@router.post(
    "/connections/{connection_id}/disable", response_model=ConnectionOut,
    openapi_extra=perm(P.CONNECTION_MANAGE),
)  # fmt: skip
async def disable(
    connection_id: uuid.UUID, caller: CallerDep, res: ResourcesDep, rid: RequestIdDep
) -> ConnectionOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_connection(s, caller, P.CONNECTION_MANAGE, connection_id)
        await s.execute(
            text("UPDATE connections SET status = 'revoked', updated_at = now() WHERE id = :i"),
            {"i": connection_id},
        )
        await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="connection_disabled",
            payload={"connection_id": str(connection_id)}, request_id=rid,
        )  # fmt: skip
        out = await _out(s, connection_id)
    await audit.anchor(res.sessions, res.s3, res.settings, caller.tenant_id)
    return out


@router.get(
    "/connections/{connection_id}", response_model=ConnectionOut,
    openapi_extra=perm(P.CONNECTION_READ),
)  # fmt: skip
async def get_connection(
    connection_id: uuid.UUID, caller: CallerDep, res: ResourcesDep
) -> ConnectionOut:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await _authorize_connection(s, caller, P.CONNECTION_READ, connection_id)
        return await _out(s, connection_id)


@router.get(
    "/clients/{client_id}/connections", response_model=Page[ConnectionOut],
    openapi_extra=perm(P.CONNECTION_READ),
)  # fmt: skip
async def list_client_connections(
    client_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[ConnectionOut]:
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.CONNECTION_READ, Scope("client", client_id))
        return await _list(s, client_id, decode(cursor), limit)


@router.get(
    "/matters/{matter_id}/connections", response_model=Page[ConnectionOut],
    openapi_extra=perm(P.JOB_START),
)  # fmt: skip
async def list_matter_connections(
    matter_id: uuid.UUID,
    caller: CallerDep,
    res: ResourcesDep,
    cursor: CursorQ = None,
    limit: LimitQ = 50,
) -> Page[ConnectionOut]:
    """The client's connections a job on this matter may use (decision a: matters reference them)."""
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        await authorize(s, caller, P.JOB_START, Scope("matter", matter_id))
        client: uuid.UUID = (
            await s.execute(text("SELECT client_id FROM matters WHERE id = :m"), {"m": matter_id})
        ).scalar_one()
        return await _list(s, client, decode(cursor), limit)


async def _list(
    s: AsyncSession, client_id: uuid.UUID, after: uuid.UUID | None, limit: int
) -> Page[ConnectionOut]:
    rows = (
        await s.execute(
            text(SELECT_PAGE),
            {"c": client_id, "after": after, "n": limit + 1},
        )
    ).all()
    return page_of([ConnectionOut(**r._mapping) for r in rows], limit, lambda c: c.id)


def _pipeline(res: Resources) -> Pipeline:
    return Pipeline(res.sessions, res.s3, res.settings, next(iter(res.connectors.values())))


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value)
