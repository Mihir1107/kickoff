"""The platform's client-authentication keys, published for IdP administrators (M17 step 3b).

- ``GET /.well-known/edisc-client-jwks.json``: the public JWKS (Okta: upload it or register its URL).
- ``GET /.well-known/edisc-client-cert.pem``: a self-signed X.509 certificate per key over the same
  public key (Entra ID's app registration takes a certificate).

Both list every configured key, the signing one first (a rollover publishes two). Only public parts:
a test compares them with the signer and parses them for private material. Served on the BASE host
only (``api_base_domain``): a tenant host answers 404, so nothing here varies by tenant.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from edisc_api.app import ResourcesDep
from edisc_api.authz import public
from edisc_api.error_codes import E
from edisc_api.errors import ApiError

router = APIRouter()
CACHE = "public, max-age=300"


class JwkOut(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    kty: str
    use: str
    alg: str
    kid: str
    n: str
    e: str
    x5c: list[str]
    x5t: str
    x5t_s256: str = Field(alias="x5t#S256")


class JwksOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[JwkOut]


def _base_host_only(request: Request, res: ResourcesDep) -> None:
    host = (request.headers.get("host") or "").split(":", 1)[0].lower()
    if host != res.settings.api_base_domain.lower():
        raise ApiError(404, "not_found", "not found")


@router.get(
    "/.well-known/edisc-client-jwks.json", response_model=JwksOut, response_model_by_alias=True,
    openapi_extra=public(E.NOT_FOUND),
)  # fmt: skip
async def client_jwks(request: Request, res: ResourcesDep, response: Response) -> JwksOut:
    _base_host_only(request, res)
    response.headers["cache-control"] = CACHE
    return JwksOut.model_validate(await res.signer.jwks())


@router.get(
    "/.well-known/edisc-client-cert.pem", response_class=Response, openapi_extra=public(E.NOT_FOUND),
    responses={200: {"content": {"application/x-pem-file": {}}}},
)  # fmt: skip
async def client_certificate(request: Request, res: ResourcesDep) -> Response:
    _base_host_only(request, res)
    return Response(
        await res.signer.certificates_pem(),
        media_type="application/x-pem-file",
        headers={"cache-control": CACHE},
    )
