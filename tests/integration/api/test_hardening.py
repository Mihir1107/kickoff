"""M13.7: OpenAPI and response models, request ids, auth-failure throttling."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi.routing import APIRoute
from sqlalchemy import text

from edisc_api.app import create_app
from edisc_db.session import tenant_tx

from .conftest import Api, TenantCtx


def test_openapi_builds_and_every_route_has_a_typed_response(api_settings: Any) -> None:
    app = create_app(api_settings)
    schema = app.openapi()
    assert schema["paths"]
    untyped = [
        f"{sorted(r.methods)} {r.path}"
        for r in app.routes
        if isinstance(r, APIRoute)
        and r.path.startswith("/v1")
        and r.response_model is None
        and r.status_code != 204
        and r.path != "/v1/evidence/{evidence_id}/content"  # streams the pinned bytes
    ]
    assert untyped == []


async def test_request_ids_reach_the_response_and_the_audit_trail(
    api: Api, tenant: TenantCtx
) -> None:
    rid = f"req-{uuid.uuid4().hex}"
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        mine = await c.post("/v1/clients", json={"name": "x"}, headers={"x-request-id": rid})
        generated = await c.get("/v1/me")
        weird = await c.get("/v1/me", headers={"x-request-id": "bad id; drop table"})
    assert mine.headers["x-request-id"] == rid
    assert uuid.UUID(generated.headers["x-request-id"])
    assert weird.headers["x-request-id"] != "bad id; drop table"
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        payload = (
            await s.execute(
                text(
                    "SELECT payload FROM custody_events WHERE stream_id = :t AND event_type = 'audit.client_created'"
                ),
                {"t": tenant.tenant_id},
            )
        ).scalar_one()
    assert payload["request_id"] == rid


async def test_repeated_auth_failures_are_throttled_per_address_and_host(
    api: Api, tenant: TenantCtx
) -> None:
    api.resources.settings = api.settings.model_copy(update={"api_auth_failures_per_minute": 3})
    api.settings = api.resources.settings
    transport = httpx.ASGITransport(
        app=create_app(api.settings, api.resources),
        client=(f"10.9.{uuid.uuid4().int % 250}.7", 1234),
    )
    base = f"http://{tenant.subdomain}.{api.settings.api_base_domain}"
    async with httpx.AsyncClient(transport=transport, base_url=base) as c:
        statuses = [
            (await c.get("/v1/me", headers={"authorization": "Bearer nope"})).status_code
            for _ in range(3)
        ]
        blocked = await c.get(
            "/v1/me", headers={"authorization": f"Bearer {tenant.token(api.settings)}"}
        )
    assert statuses == [401, 401, 401]
    assert blocked.status_code == 429
    other = httpx.ASGITransport(
        app=create_app(api.settings, api.resources), client=("10.200.1.1", 1)
    )
    async with httpx.AsyncClient(transport=other, base_url=base) as c:
        ok = await c.get(
            "/v1/me", headers={"authorization": f"Bearer {tenant.token(api.settings)}"}
        )
    assert ok.status_code == 200
