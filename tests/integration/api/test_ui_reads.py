"""The UI's read endpoints (M17 step 3, plan §3.1, §3.2, §3.4, §3.5): `/me/permissions`, `/roles`,
`GET /v1/jobs`, a job's custody events, groups and their members, and cursors bound to their
filters. Expected values come from the fixtures built here and from `authz.ROLE_PERMISSIONS`, never
from the endpoints under test. The role x route matrix for the scoped routes is
`test_authz.py::test_access_matrix`."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from edisc_api import authz
from edisc_core.ids import new_id
from edisc_core.kms import LocalKmsClient
from edisc_core.time import utc_now
from edisc_custody.log import append
from edisc_db.session import tenant_tx

from .conftest import Api, TenantCtx, add_principal, new_api_tenant
from .test_authz import PLACEMENT, build_world


async def _all(c: httpx.AsyncClient, path: str, **params: Any) -> list[dict[str, Any]]:
    """Every item of a list route, page by page (asserting each page is a 200)."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        r = await c.get(path, params={**params, **({"cursor": cursor} if cursor else {})})
        assert r.status_code == 200, r.text
        body = r.json()
        items += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return items


async def _job(
    api: Api, t: TenantCtx, matter: uuid.UUID, connection: uuid.UUID, *, status: str = "running",
    created: Any = None, finished: Any = None,
) -> uuid.UUID:  # fmt: skip
    job = new_id()
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(
            text("INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status,"
                 " connector_version, requested_by, created_at, finished_at)"
                 " VALUES (:i, :t, :m, :c, :st, '0', 'tests', COALESCE(:cr, now()), :f)"),
            {"i": job, "t": t.tenant_id, "m": matter, "c": connection, "st": status,
             "cr": created, "f": finished},
        )  # fmt: skip
    return job


# ------------------------------------------------------------------ /roles and /me/permissions
async def test_roles_are_generated_from_role_permissions(
    api: Api, tenant: TenantCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        got = {r["role"]: r["permissions"] for r in await _all(c, "/v1/roles", limit=2)}
        assert got == {r: sorted(p) for r, p in authz.ROLE_PERMISSIONS.items()}
        # the endpoint follows the table: a role edited in the fixture is what it answers
        monkeypatch.setitem(authz.ROLE_PERMISSIONS, "reviewer", frozenset({authz.P.JOB_READ}))
        monkeypatch.setitem(authz.ROLE_PERMISSIONS, "zz_extra", frozenset({authz.P.CLIENT_READ}))
        got = {r["role"]: r["permissions"] for r in await _all(c, "/v1/roles")}
        assert got["reviewer"] == ["job.read"] and got["zz_extra"] == ["client.read"]


async def test_my_permissions_list_every_scope_held_directly_or_through_a_group(
    api: Api, tenant: TenantCtx
) -> None:
    world = await build_world(api, tenant)
    pid, subject = await add_principal(
        api, tenant, roles=[("reviewer", "matter", world.m1), ("collector", "matter", world.m1),
                            ("auditor", "client", world.c2)],
    )  # fmt: skip
    admin = tenant.token(api.settings)
    async with api.client(tenant.subdomain, admin) as c:
        group = (await c.post("/v1/groups", json={"name": "G"})).json()["id"]
        assert (await c.post(f"/v1/groups/{group}/members",
                             json={"principal_id": str(pid)})).status_code == 204  # fmt: skip
        ws = (await c.post("/v1/role-assignments", json={
            "group_id": group, "role": "matter_manager", "scope_type": "workspace",
            "scope_id": str(world.w1)})).json()  # fmt: skip
        gone = (await c.post("/v1/role-assignments", json={
            "principal_id": str(pid), "role": "client_admin", "scope_type": "client",
            "scope_id": str(world.c1)})).json()  # fmt: skip
        assert (await c.post(f"/v1/role-assignments/{gone['id']}/revoke")).status_code == 200
    token = tenant.token(api.settings, subject=subject)
    async with api.client(tenant.subdomain, token) as c:
        got = await _all(c, "/v1/me/permissions", limit=1)
    expect = {
        ("matter", str(world.m1)): {"collector", "reviewer"},
        ("client", str(world.c2)): {"auditor"},
        ("workspace", str(world.w1)): {"matter_manager"},  # through the group
    }  # the revoked client_admin is not there
    assert [(g["scope_type"], g["scope_id"]) for g in got] == sorted(expect)
    for g in got:
        roles = expect[(g["scope_type"], g["scope_id"])]
        assert g["roles"] == sorted(roles)
        assert g["permissions"] == sorted(authz.permissions(roles))
    assert ws["revoked_at"] is None
    async with api.client(tenant.subdomain, admin) as c:  # the tenant admin: one tenant scope
        mine = await _all(c, "/v1/me/permissions")
    assert mine == [{"scope_type": "tenant", "scope_id": None, "roles": ["tenant_admin"],
                     "permissions": sorted(authz.Permission)}]  # fmt: skip


# ------------------------------------------------------------------ GET /v1/jobs
@pytest.mark.parametrize("role", [*PLACEMENT, "workspace_reviewer", "revoked_client_admin"])
async def test_the_jobs_list_holds_exactly_the_jobs_the_caller_may_read(
    api: Api, tenant: TenantCtx, role: str
) -> None:
    """Every listed job answers 200 on GET, every job that answers 200 is listed (§3.4)."""
    world = await build_world(api, tenant)
    jobs = [world.j1, world.j2, await _job(api, tenant, world.m1, world.conn1)]
    ids = {"c1": world.c1, "c2": world.c2, "m1": world.m1, "m2": world.m2}
    if role == "workspace_reviewer":  # a role below the matter: sees the workspace, not the jobs
        roles = [("reviewer", "workspace", world.w1)]
    elif role == "revoked_client_admin":
        roles = [("client_admin", "client", world.c1)]
    else:
        place = PLACEMENT[role]
        roles = (
            [] if place is None else [(role, place[0], None if place[1] is None else ids[place[1]])]
        )
    pid, subject = await add_principal(api, tenant, roles=roles)
    if role == "revoked_client_admin":
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            await s.execute(text("UPDATE role_assignments SET revoked_at = now(), revoked_by = 't'"
                                 " WHERE principal_id = :p"), {"p": pid})  # fmt: skip
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        listed = {uuid.UUID(j["id"]) for j in await _all(c, "/v1/jobs", limit=1)}
        readable = {j for j in jobs if (await c.get(f"/v1/jobs/{j}")).status_code == 200}
    assert listed == readable
    expect = {  # independent statement: a role at the job's matter, its client or the tenant
        "tenant_admin": set(jobs), "client_admin": {jobs[0], jobs[2]}, "auditor": {jobs[0], jobs[2]},
        "matter_manager": {jobs[0], jobs[2]}, "collector": {jobs[0], jobs[2]},
        "reviewer": {jobs[0], jobs[2]}, "nobody": set(), "workspace_reviewer": set(),
        "revoked_client_admin": set(),
    }[role]  # fmt: skip
    assert listed == expect


async def test_the_jobs_list_filters_orders_and_pages(api: Api, tenant: TenantCtx) -> None:
    world = await build_world(api, tenant)
    now = utc_now()
    at = [now - timedelta(hours=h) for h in range(6)]  # at[0] newest
    a = await _job(api, tenant, world.m1, world.conn1, status="completed", created=at[5],
                   finished=at[4])  # fmt: skip
    b = await _job(api, tenant, world.m1, world.conn1, status="completed_against_archive",
                   created=at[3], finished=at[2])  # fmt: skip
    c_ = await _job(api, tenant, world.m2, world.conn2, status="failed", created=at[1])
    tie1 = await _job(api, tenant, world.m2, world.conn2, created=at[0])
    tie2 = await _job(api, tenant, world.m2, world.conn2, created=at[0])
    tie3 = await _job(api, tenant, world.m2, world.conn2, created=at[0])  # a page ends in the tie
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        await s.execute(text("UPDATE connections SET source = 'slack_export' WHERE id = :c"),
                        {"c": world.conn2})  # fmt: skip
    mine = {a, b, c_, tie1, tie2, tie3, world.j1, world.j2}

    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:

        async def ids(**params: Any) -> list[uuid.UUID]:
            return [uuid.UUID(j["id"]) for j in await _all(c, "/v1/jobs", limit=2, **params)
                    if uuid.UUID(j["id"]) in mine]  # fmt: skip

        everything = await ids()
        assert set(everything) == mine
        assert everything[:3] == sorted([tie1, tie2, tie3], reverse=True)  # same time: id DESC
        assert everything.index(c_) < everything.index(b) < everything.index(a)
        # order: created_at DESC, id DESC, across page boundaries (limit 2)
        rows = [j for j in await _all(c, "/v1/jobs", limit=2) if uuid.UUID(j["id"]) in mine]
        keys = [(j["created_at"], j["id"]) for j in rows]
        assert keys == sorted(keys, reverse=True)
        assert await ids(status="failed") == [c_]
        assert await ids(clean_basis="source") == [a]
        assert await ids(clean_basis="archive") == [b]
        assert await ids(clean_basis="source", status="failed") == []
        assert set(await ids(client_id=str(world.c2))) == {c_, tie1, tie2, tie3, world.j2}
        assert set(await ids(matter_id=str(world.m1))) == {a, b, world.j1}
        assert set(await ids(connection_id=str(world.conn2))) == {c_, tie1, tie2, tie3, world.j2}
        assert set(await ids(source="slack_export")) == {c_, tie1, tie2, tie3, world.j2}
        assert await ids(created_from=at[4].isoformat(), created_to=at[1].isoformat()) == [b]
        assert set(await ids(finished_from=at[4].isoformat())) == {a, b}
        assert (await c.get("/v1/jobs", params={"created_from": "2026-01-01T00:00:00"})
                ).status_code == 422  # a naive time is refused, never assumed UTC  # fmt: skip
        assert (await c.get("/v1/jobs", params={"status": "nonsense"})).status_code == 422


async def test_rows_existing_at_the_first_page_are_returned_once_under_inserts(
    api: Api, tenant: TenantCtx
) -> None:
    world = await build_world(api, tenant)
    for _ in range(5):
        await _job(api, tenant, world.m1, world.conn1)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        first = (await c.get("/v1/jobs", params={"limit": 2})).json()
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            before = set((await s.execute(text("SELECT id FROM collection_jobs"))).scalars())
        seen = [uuid.UUID(j["id"]) for j in first["items"]]
        cursor = first["next_cursor"]
        while cursor:
            await _job(api, tenant, world.m1, world.conn1)  # newer than every remaining row
            page = (await c.get("/v1/jobs", params={"limit": 2, "cursor": cursor})).json()
            seen += [uuid.UUID(j["id"]) for j in page["items"]]
            cursor = page["next_cursor"]
    assert sorted(seen) == sorted(before) and len(seen) == len(set(seen))


async def test_a_cursor_is_bound_to_its_route_path_and_filters(api: Api, tenant: TenantCtx) -> None:
    world = await build_world(api, tenant)
    for _ in range(3):
        await _job(api, tenant, world.m1, world.conn1, status="failed")
        await _job(api, tenant, world.m2, world.conn2, status="failed")
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        cursor = (await c.get("/v1/jobs", params={"limit": 1, "status": "failed"})).json()[
            "next_cursor"
        ]

        async def code(path: str, **params: Any) -> tuple[int, str | None]:
            r = await c.get(path, params={**params, "cursor": cursor})
            return r.status_code, r.json().get("error")

        assert await code("/v1/jobs", status="failed", limit=3) == (200, None)  # limit is free
        assert await code("/v1/jobs", status="running") == (422, "cursor_mismatch")
        assert await code("/v1/jobs") == (422, "cursor_mismatch")
        assert await code("/v1/jobs", status="failed", matter_id=str(world.m1)) == (
            422, "cursor_mismatch")  # fmt: skip
        assert await code("/v1/clients") == (422, "cursor_mismatch")
        cursor = (await c.get(f"/v1/matters/{world.m1}/jobs", params={"limit": 1})).json()[
            "next_cursor"
        ]
        assert await code(f"/v1/matters/{world.m1}/jobs") == (200, None)
        assert await code(f"/v1/matters/{world.m2}/jobs") == (422, "cursor_mismatch")
        for _ in range(3):  # two routes with the same kind of position and no parameters
            await add_principal(api, tenant)
            await c.post("/v1/groups", json={"name": f"G {uuid.uuid4()}"})
        cursor = (await c.get("/v1/principals", params={"limit": 1})).json()["next_cursor"]
        assert await code("/v1/principals") == (200, None)
        assert await code("/v1/groups") == (422, "cursor_mismatch")
        assert (await c.get("/v1/jobs", params={"cursor": "not-a-cursor"})).json()["error"] == (
            "unprocessable"
        )


# ------------------------------------------------------------------ custody events
async def test_custody_events_page_by_seq_within_a_byte_budget(api: Api, tenant: TenantCtx) -> None:
    world = await build_world(api, tenant)
    job = world.j1
    big = "x" * 300_000  # four of these exceed the 1 MiB page budget
    sizes = ["s", big, big, big, big, "s", "x" * 1_200_000, "s"]
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        for i, blob in enumerate(sizes):
            await append(s, tenant_id=tenant.tenant_id, stream_id=job, job_id=job,
                         event_type="note", actor="tests", payload={"i": i, "blob": blob})  # fmt: skip
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        pages: list[list[int]] = []
        cursor = None
        while True:
            r = await c.get(f"/v1/jobs/{job}/custody/events",
                            params={"limit": 500, **({"cursor": cursor} if cursor else {})})  # fmt: skip
            assert r.status_code == 200
            body = r.json()
            pages.append([e["payload"]["i"] for e in body["items"]])
            assert sum(len(e["payload"]["blob"]) for e in body["items"]) <= (1 << 20) or len(
                body["items"]) == 1  # fmt: skip
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert [i for p in pages for i in p] == list(range(len(sizes)))
        assert pages[0] == [0, 1, 2, 3]  # 0..3 fit, 4 would pass 1 MiB
        assert [6] in pages  # a single event over the budget still makes a page alone
        events = (await c.get(f"/v1/jobs/{job}/custody/events", params={"limit": 2})).json()
        first = events["items"]
        assert [e["seq"] for e in first] == [1, 2] and first[1]["prev_hash"] == first[0][
            "event_hash"
        ]
        assert (
            await c.get(f"/v1/jobs/{job}/custody/events", params={"limit": 501})
        ).status_code == 422


async def test_another_tenants_jobs_and_events_do_not_exist_here(
    api: Api, tenant: TenantCtx, kms: LocalKmsClient
) -> None:
    world = await build_world(api, tenant)
    other = await new_api_tenant(api, kms)
    other_world = await build_world(api, other)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        listed = {j["id"] for j in await _all(c, "/v1/jobs")}
        assert str(world.j1) in listed and str(other_world.j1) not in listed
        r = await c.get(f"/v1/jobs/{other_world.j1}/custody/events")
        assert r.status_code == 404
        assert (await c.get(f"/v1/groups/{other_world.group}/members")).status_code == 404
        assert (await c.get("/v1/jobs", params={"matter_id": str(other_world.m1)})).json()[
            "items"
        ] == []
    # the other tenant's admin token on this host: the generic 401
    async with api.client(tenant.subdomain, other.token(api.settings)) as c:
        assert (await c.get("/v1/jobs")).status_code == 401
        assert (await c.get("/v1/me/permissions")).status_code == 401


# ------------------------------------------------------------------ groups
async def test_groups_and_members_list_current_and_ended_memberships(
    api: Api, tenant: TenantCtx
) -> None:
    p1, _ = await add_principal(api, tenant)
    p2, _ = await add_principal(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        g = (await c.post("/v1/groups", json={"name": "Lit", "external_id": "lit"})).json()
        other = (await c.post("/v1/groups", json={"name": "Other"})).json()
        for p in (p1, p2):
            assert (await c.post(f"/v1/groups/{g['id']}/members",
                                 json={"principal_id": str(p)})).status_code == 204  # fmt: skip
        assert (await c.post(f"/v1/groups/{g['id']}/members/{p2}/remove")).status_code == 204
        groups = {x["id"]: x for x in await _all(c, "/v1/groups", limit=1)}
        assert groups[g["id"]] == g and groups[other["id"]] == other
        current = await _all(c, f"/v1/groups/{g['id']}/members")
        assert [m["principal_id"] for m in current] == [str(p1)]
        everyone = await _all(c, f"/v1/groups/{g['id']}/members", include_removed="true", limit=1)
        assert {m["principal_id"]: m["removed_at"] is not None for m in everyone} == {
            str(p1): False, str(p2): True}  # fmt: skip
        assert (await c.get(f"/v1/groups/{uuid.uuid4()}/members")).status_code == 404
