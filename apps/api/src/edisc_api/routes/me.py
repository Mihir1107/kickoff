"""The authenticated caller (no permission needed beyond being an active principal of the tenant)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from edisc_api.app import CallerDep

router = APIRouter(prefix="/v1")


class Me(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: uuid.UUID
    kind: str
    subject: str
    issuer: str


@router.get("/me", response_model=Me, openapi_extra={"x-permission": "authenticated"})
async def me(caller: CallerDep) -> Me:
    return Me(
        principal_id=caller.principal_id,
        kind=caller.kind,
        subject=caller.subject,
        issuer=caller.issuer,
    )
