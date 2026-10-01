"""ADR 0013 section 3: scoped roles, 404 vs 403, groups, revocation, route coverage, audit."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from edisc_api.app import create_app
from edisc_api.authz import ROLE_PERMISSIONS, P, Permission
from edisc_core.kms import LocalKmsClient
from edisc_core.time import utc_now
from edisc_db.session import tenant_tx

from .conftest import Api, TenantCtx, add_principal


def test_every_route_declares_a_permission(api_settings: Any) -> None:
    app = create_app(api_settings)
    missing = [
        f"{sorted(r.methods)} {r.path}"  # type: ignore[attr-defined]
        for r in app.routes
        if getattr(r, "path", "").startswith("/v1")
        and not (getattr(r, "openapi_extra", None) or {}).get("x-permission")
    ]
    assert missing == []


@dataclass
class World:
    c1: uuid.UUID
    c2: uuid.UUID
    m1: uuid.UUID
    m2: uuid.UUID
    w1: uuid.UUID


async def build_world(api: Api, t: TenantCtx) -> World:
    admin = t.token(api.settings)
    retention = (utc_now() + timedelta(days=30)).isoformat()
    async with api.client(t.subdomain, admin) as c:
        c1 = (await c.post("/v1/clients", json={"name": "Client One"})).json()["id"]
        c2 = (await c.post("/v1/clients", json={"name": "Client Two"})).json()["id"]
        m1 = (
            await c.post(
                f"/v1/clients/{c1}/matters", json={"name": "M1", "retention_until": retention}
            )
        ).json()["id"]
        m2 = (
            await c.post(
                f"/v1/clients/{c2}/matters", json={"name": "M2", "retention_until": retention}
            )
        ).json()["id"]
        w1 = (await c.post(f"/v1/matters/{m1}/workspaces", json={"name": "W1"})).json()["id"]
    return World(*(uuid.UUID(x) for x in (c1, c2, m1, m2, w1)))


# where each role is assigned in the matrix
PLACEMENT = {
    "tenant_admin": ("tenant", None),
    "client_admin": ("client", "c1"),
    "matter_manager": ("matter", "m1"),
    "collector": ("matter", "m1"),
    "reviewer": ("matter", "m1"),
    "auditor": ("client", "c1"),
    "nobody": None,
}
# target object -> its chain (self first) in the world
CHAIN = {
    "tenant": ["tenant"],
    "c1": ["c1", "tenant"],
    "c2": ["c2", "tenant"],
    "m1": ["m1", "c1", "tenant"],
    "m2": ["m2", "c2", "tenant"],
    "w1": ["w1", "m1", "c1", "tenant"],
}
VISIBILITY = {"tenant": P.CLIENT_READ, "c1": P.CLIENT_READ, "c2": P.CLIENT_READ,
              "m1": P.MATTER_READ, "m2": P.MATTER_READ, "w1": P.WORKSPACE_READ}  # fmt: skip


def expected(role: str, permission: Permission, target: str) -> int:
    """Independent statement of the rule: allowed if the role holds the permission at the target or
    above; 403 if it can at least see the target; else 404."""
    place = PLACEMENT[role]
    if place is None:
        return 404
    where = "tenant" if place[0] == "tenant" else place[1]
    held = ROLE_PERMISSIONS[role] if where in CHAIN[target] else frozenset()
    if permission in held:
        return 0  # success (2xx)
    return 403 if VISIBILITY[target] in held else 404


# (permission, target, method, path template, json body)
CASES: list[tuple[Permission, str, str, str, dict[str, Any] | None]] = [
    (P.CLIENT_CREATE, "tenant", "post", "/v1/clients", {"name": "x"}),
    (P.CLIENT_READ, "c1", "get", "/v1/clients/{c1}", None),
    (P.CLIENT_READ, "c2", "get", "/v1/clients/{c2}", None),
    (
        P.MATTER_CREATE,
        "c1",
        "post",
        "/v1/clients/{c1}/matters",
        {"name": "x", "retention_until": "RET"},
    ),
    (
        P.MATTER_CREATE,
        "c2",
        "post",
        "/v1/clients/{c2}/matters",
        {"name": "x", "retention_until": "RET"},
    ),
    (P.MATTER_READ, "m1", "get", "/v1/matters/{m1}", None),
    (P.MATTER_READ, "m2", "get", "/v1/matters/{m2}", None),
    (P.WORKSPACE_CREATE, "m1", "post", "/v1/matters/{m1}/workspaces", {"name": "x"}),
    (P.WORKSPACE_CREATE, "m2", "post", "/v1/matters/{m2}/workspaces", {"name": "x"}),
    (P.MATTER_READ, "m1", "get", "/v1/matters/{m1}/workspaces", None),
    (P.TENANT_ADMIN, "tenant", "get", "/v1/principals", None),
    (P.TENANT_ADMIN, "tenant", "get", "/v1/role-assignments", None),
]


@pytest.mark.parametrize("role", list(PLACEMENT))
async def test_access_matrix(api: Api, tenant: TenantCtx, role: str) -> None:
    world = await build_world(api, tenant)
    ids = {"c1": world.c1, "c2": world.c2, "m1": world.m1, "m2": world.m2, "w1": world.w1}
    place = PLACEMENT[role]
    roles = [] if place is None else [(role, place[0], None if place[1] is None else ids[place[1]])]
    _, subject = await add_principal(api, tenant, roles=roles)
    token = tenant.token(api.settings, subject=subject)
    retention = (utc_now() + timedelta(days=30)).isoformat()
    failures = []
    async with api.client(tenant.subdomain, token) as c:
        for permission, target, method, template, body in CASES:
            path = template.format(**ids)
            payload = (
                None
                if body is None
                else {k: (retention if v == "RET" else v) for k, v in body.items()}
            )
            r = await getattr(c, method)(path, **({"json": payload} if payload is not None else {}))
            want = expected(role, permission, target)
            ok = (200 <= r.status_code < 300) if want == 0 else r.status_code == want
            if not ok:
                failures.append(
                    f"{method.upper()} {template} -> {r.status_code}, expected {want or '2xx'}"
                )
    assert failures == [], f"{role}: {failures}"


async def test_group_claims_grant_roles_and_revocation_removes_them(
    api: Api, tenant: TenantCtx
) -> None:
    world = await build_world(api, tenant)
    admin = tenant.token(api.settings)
    pid, subject = await add_principal(api, tenant)
    async with api.client(tenant.subdomain, admin) as c:
        group = (
            await c.post("/v1/groups", json={"name": "Lit team", "external_id": "lit-team"})
        ).json()
        grant = (
            await c.post(
                "/v1/role-assignments",
                json={
                    "group_id": group["id"],
                    "role": "matter_manager",
                    "scope_type": "matter",
                    "scope_id": str(world.m1),
                },
            )
        ).json()
    with_claim = tenant.token(api.settings, subject=subject, groups=["lit-team"])
    without = tenant.token(api.settings, subject=subject)
    async with api.client(tenant.subdomain, with_claim) as c:
        assert (await c.get(f"/v1/matters/{world.m1}")).status_code == 200
    async with api.client(tenant.subdomain, without) as c:
        assert (await c.get(f"/v1/matters/{world.m1}")).status_code == 404
    async with api.client(tenant.subdomain, admin) as c:  # local membership works without the claim
        assert (
            await c.post(f"/v1/groups/{group['id']}/members", json={"principal_id": str(pid)})
        ).status_code == 204
    async with api.client(tenant.subdomain, without) as c:
        assert (await c.get(f"/v1/matters/{world.m1}")).status_code == 200
    async with api.client(tenant.subdomain, admin) as c:
        revoked = await c.post(f"/v1/role-assignments/{grant['id']}/revoke")
        assert (
            revoked.status_code == 200 and revoked.json()["revoked_by"] == f"user:{tenant.admin_id}"
        )
    async with api.client(tenant.subdomain, with_claim) as c:
        assert (await c.get(f"/v1/matters/{world.m1}")).status_code == 404


async def test_changes_are_audited_with_the_acting_principal(api: Api, tenant: TenantCtx) -> None:
    await build_world(api, tenant)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT event_type, actor, payload FROM custody_events WHERE stream_id = :t ORDER BY seq"
                ),
                {"t": tenant.tenant_id},
            )
        ).all()
    types = [r.event_type for r in rows]
    assert types == [
        "audit.tenant_onboarded",
        "audit.client_created",
        "audit.client_created",
        "audit.matter_created",
        "audit.matter_created",
        "audit.workspace_created",
    ]
    assert all(r.actor == f"user:{tenant.admin_id}" for r in rows[1:])
    assert all("request_id" in r.payload for r in rows[1:])


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/clients", {"name": "x", "tenant_id": "00000000-0000-0000-0000-000000000000"}),
        ("/v1/principals", {"issuer": "i", "subject": "s", "display_name": "d", "tenant_id": "x"}),
    ],
)
async def test_request_bodies_with_a_tenant_id_are_rejected(
    api: Api, tenant: TenantCtx, path: str, body: dict[str, Any]
) -> None:
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        r = await c.post(path, json=body)
    assert r.status_code == 422


async def test_cursor_pagination_returns_every_existing_row_once_under_concurrent_inserts(
    api: Api, tenant: TenantCtx
) -> None:
    import asyncio

    admin = tenant.token(api.settings)
    async with api.client(tenant.subdomain, admin) as c:
        for i in range(7):
            assert (await c.post("/v1/clients", json={"name": f"c{i}"})).status_code == 201
        before = set()
        first = (await c.get("/v1/clients", params={"limit": 3})).json()
        seen = [x["id"] for x in first["items"]]
        cursor = first["next_cursor"]
        # concurrent inserts while paging
        await asyncio.gather(*(c.post("/v1/clients", json={"name": f"late{i}"}) for i in range(4)))
        while cursor:
            page = (await c.get("/v1/clients", params={"limit": 3, "cursor": cursor})).json()
            seen += [x["id"] for x in page["items"]]
            cursor = page["next_cursor"]
        bad = await c.get("/v1/clients", params={"cursor": "not-a-cursor"})
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        before = {str(r) for r in (await s.execute(text("SELECT id FROM clients"))).scalars()}
    assert len(seen) == len(set(seen))  # no duplicates
    assert len(seen) >= 8  # the default client + 7 created before paging: none skipped
    assert set(seen) <= before
    assert bad.status_code == 422


async def test_list_clients_shows_only_visible_clients(
    api: Api, tenant: TenantCtx, kms: LocalKmsClient
) -> None:
    world = await build_world(api, tenant)
    _, subject = await add_principal(api, tenant, roles=[("collector", "matter", world.m2)])
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        clients = [x["id"] for x in (await c.get("/v1/clients")).json()["items"]]
        matters = (await c.get(f"/v1/clients/{world.c2}/matters")).json()["items"]
        hidden = await c.get(f"/v1/clients/{world.c1}/matters")
    assert clients == [str(world.c2)]
    assert [m["id"] for m in matters] == [str(world.m2)]
    assert hidden.status_code == 404
