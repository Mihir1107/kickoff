"""Legal holds in the database (M18 §1.1, §1.2, §1.4; D1-D4; threats T3, T4, T14, T16), through the
real API and Postgres.

A hold blocks closing (the API's 409 and a trigger for any other path), is placed and read under its
permissions, is released only by two different tenant admins (never with one admin, never by the
requester, never after 72 h), starts a cooling-off, and answers one question for every caller:
``edisc_custody.holds``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text

from edisc_core.kms import LocalKmsClient
from edisc_db.session import tenant_tx

from ..conftest import Connect
from .conftest import Api, TenantCtx, add_principal, new_api_tenant


@pytest.fixture
async def other_tenant(api: Api, kms: LocalKmsClient) -> TenantCtx:
    return await new_api_tenant(api, kms)


async def _matter(c: Any, client_id: uuid.UUID | str, name: str = "M") -> str:
    r = await c.post(f"/v1/clients/{client_id}/matters",
                     json={"name": name, "retention_until": "2099-01-01T00:00:00Z"})  # fmt: skip
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _second_admin(api: Api, t: TenantCtx) -> str:
    _, subject = await add_principal(api, t, roles=[("tenant_admin", "tenant", None)])
    return subject


async def _events(api: Api, t: TenantCtx, prefix: str) -> list[dict[str, Any]]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        rows = (await s.execute(text(
            "SELECT event_type, payload FROM custody_events WHERE stream_id = :t AND event_type LIKE :p"
            " ORDER BY seq"), {"t": t.tenant_id, "p": f"audit.{prefix}%"})).all()  # fmt: skip
    return [{"type": r.event_type, **r.payload} for r in rows]


async def test_a_matter_hold_blocks_closing_until_two_admins_release_it(
    api: Api, tenant: TenantCtx
) -> None:
    second = await _second_admin(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        m = await _matter(a, tenant.default_client_id)
        r = await a.post(
            f"/v1/matters/{m}/legal-holds", json={"reason": "litigation", "reference": "D-1"}
        )
        assert r.status_code == 201, r.text
        hold = r.json()
        assert (hold["active"], hold["scope"], hold["matter_id"]) == (True, "matter", m)
        assert hold["flags"]["objects_in_scope"] is None and hold["flags"]["pending"] is None
        assert (await a.get(f"/v1/matters/{m}")).json()["held"] is True
        r = await a.post(f"/v1/matters/{m}/close")
        assert (r.status_code, r.json()["error"]) == (409, "legal_hold")
        # one person cannot release it
        r = await a.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "settled"})
        assert r.status_code == 200 and r.json()["active"] is True
        assert r.json()["release_request"]["reason"] == "settled"
        r = await a.post(f"/v1/legal-holds/{hold['id']}/release/approve")
        assert (r.status_code, r.json()["error"]) == (403, "same_principal")
        assert (await a.post(f"/v1/matters/{m}/close")).status_code == 409
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=second)) as b:
        r = await b.post(f"/v1/legal-holds/{hold['id']}/release/approve")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["active"] is False and out["release_request"] is None
        assert out["release_reason"] == "settled"
        cooling = out["cooling_off_until"]
        assert (await b.post(f"/v1/legal-holds/{hold['id']}/release/approve")).status_code == 409
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        assert (await a.get(f"/v1/matters/{m}")).json()["held"] is False
        assert (await a.post(f"/v1/matters/{m}/close")).status_code == 200
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        row = (await s.execute(text(
            "SELECT released_at, cooling_off_until FROM legal_holds WHERE id = :h"),
            {"h": uuid.UUID(hold["id"])})).one()  # fmt: skip
    assert row.cooling_off_until - row.released_at == timedelta(days=7)  # D4 default
    assert cooling is not None
    kinds = [e["type"] for e in await _events(api, tenant, "legal_hold")]
    assert kinds == ["audit.legal_hold_placed", "audit.legal_hold_release_requested",
                     "audit.legal_hold_released"], kinds  # fmt: skip
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        alerts = set((await s.execute(text(
            "SELECT kind FROM alerts WHERE kind LIKE 'legal_hold%'"))).scalars())  # fmt: skip
    assert alerts == {"legal_hold_placed", "legal_hold_release_requested", "legal_hold_released"}


async def test_a_tenant_with_one_admin_cannot_release_at_all(api: Api, tenant: TenantCtx) -> None:
    """D3: never an operator path; the tenant adds a second admin first."""
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        m = await _matter(a, tenant.default_client_id)
        hold = (await a.post(f"/v1/matters/{m}/legal-holds", json={"reason": "x"})).json()
        r = await a.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "y"})
        assert (r.status_code, r.json()["error"]) == (409, "second_admin_required")
    # a deactivated or service admin does not count
    pid, _ = await add_principal(
        api, tenant, roles=[("tenant_admin", "tenant", None)], kind="service"
    )
    del pid
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        r = await a.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "y"})
        assert r.status_code == 409


async def test_a_release_request_expires_after_72_hours(
    api: Api, tenant: TenantCtx, connect: Connect
) -> None:
    second = await _second_admin(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        m = await _matter(a, tenant.default_client_id)
        hold = (await a.post(f"/v1/matters/{m}/legal-holds", json={"reason": "x"})).json()
        assert (
            await a.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "y"})
        ).status_code == 200
    su = await connect("superuser")
    try:  # the request is 73 hours old (the identity columns are immutable: replace the row)
        await su.execute("SET session_replication_role = replica")
        await su.execute(
            "UPDATE edisc.legal_hold_release_requests SET requested_at = now() - interval '73 hours',"
            " expires_at = now() - interval '1 hour' WHERE hold_id = $1",
            uuid.UUID(hold["id"]),
        )
    finally:
        await su.close()
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=second)) as b:
        r = await b.post(f"/v1/legal-holds/{hold['id']}/release/approve")
        assert r.status_code == 409 and "expired" in r.json()["detail"]
        assert (await b.get(f"/v1/legal-holds/{hold['id']}")).json()["active"] is True
        # a new request may be made, and approved by the other admin
        assert (
            await b.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "z"})
        ).status_code == 200
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        assert (await a.post(f"/v1/legal-holds/{hold['id']}/release/approve")).status_code == 200
    kinds = [e["type"] for e in await _events(api, tenant, "legal_hold_release")]
    assert kinds == ["audit.legal_hold_release_requested", "audit.legal_hold_release_expired",
                     "audit.legal_hold_release_requested", "audit.legal_hold_released"], kinds  # fmt: skip


async def test_client_holds_cover_every_matter_and_block_the_client_close(
    api: Api, tenant: TenantCtx
) -> None:
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        client = (await a.post("/v1/clients", json={"name": "Held"})).json()["id"]
        m = await _matter(a, client)
        hold = (await a.post(f"/v1/clients/{client}/legal-holds", json={"reason": "x"})).json()
        assert hold["scope"] == "client"
        r = await a.post(f"/v1/matters/{m}/close")  # a client hold covers its matters
        assert (r.status_code, r.json()["error"]) == (409, "legal_hold")
        assert (await a.get(f"/v1/clients/{client}")).json()["held"] is True
        assert (await a.get(f"/v1/matters/{m}")).json()["held"] is True
        # a matter hold on a closed matter (allowed) still blocks closing its client
        other = (await a.post("/v1/clients", json={"name": "Other"})).json()["id"]
        m2 = await _matter(a, other)
        assert (await a.post(f"/v1/matters/{m2}/close")).status_code == 200
        assert (
            await a.post(f"/v1/matters/{m2}/legal-holds", json={"reason": "late"})
        ).status_code == 201
        r = await a.post(f"/v1/clients/{other}/close")
        assert (r.status_code, r.json()["error"]) == (409, "legal_hold")


@pytest.mark.parametrize("role", ["app", "superuser"])
async def test_the_trigger_refuses_a_close_under_hold_on_any_path(
    api: Api, tenant: TenantCtx, connect: Connect, role: str
) -> None:
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        m = await _matter(a, tenant.default_client_id)
        client = (await a.post("/v1/clients", json={"name": "C"})).json()["id"]
        assert (
            await a.post(f"/v1/matters/{m}/legal-holds", json={"reason": "x"})
        ).status_code == 201
        assert (
            await a.post(f"/v1/clients/{client}/legal-holds", json={"reason": "x"})
        ).status_code == 201
    conn = await connect(role)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant.tenant_id)
            )
            with pytest.raises(asyncpg.PostgresError) as exc:
                await conn.execute(
                    "UPDATE matters SET closed_at = now(), closed_by = 'sql' WHERE id = $1",
                    uuid.UUID(m),
                )
            assert exc.value.sqlstate == "EA005"
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant.tenant_id)
            )
            with pytest.raises(asyncpg.PostgresError) as exc:
                await conn.execute(
                    "UPDATE clients SET closed_at = now(), closed_by = 'sql' WHERE id = $1",
                    uuid.UUID(client),
                )
            assert exc.value.sqlstate == "EA005"
    finally:
        await conn.close()


async def test_two_people_and_immutability_are_database_rules(
    api: Api, tenant: TenantCtx, connect: Connect
) -> None:
    second = await _second_admin(api, tenant)
    del second
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        m = await _matter(a, tenant.default_client_id)
        hold = (await a.post(f"/v1/matters/{m}/legal-holds", json={"reason": "x"})).json()
        assert (
            await a.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "y"})
        ).status_code == 200
    app = await connect("app")
    try:
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant.tenant_id))
            with pytest.raises(asyncpg.CheckViolationError):  # the requester approves itself
                await app.execute(
                    "UPDATE legal_hold_release_requests SET decision = 'approved', decided_at = now(),"
                    " decided_by = 'x', decided_by_principal = requested_by_principal WHERE hold_id = $1",
                    uuid.UUID(hold["id"]),
                )
        for sql in ("UPDATE legal_holds SET reason = 'changed' WHERE id = $1",
                    "UPDATE legal_holds SET matter_id = NULL, client_id = matter_id WHERE id = $1",
                    "DELETE FROM legal_holds WHERE id = $1"):  # fmt: skip
            async with app.transaction():
                await app.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant.tenant_id)
                )
                with pytest.raises(asyncpg.PostgresError):
                    await app.execute(sql, uuid.UUID(hold["id"]))
    finally:
        await app.close()


async def test_permissions_reads_and_tenant_isolation(
    api: Api, tenant: TenantCtx, other_tenant: TenantCtx
) -> None:
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        m = await _matter(a, tenant.default_client_id)
        m_other = await _matter(a, tenant.default_client_id, "N")
        hold = (await a.post(f"/v1/matters/{m}/legal-holds", json={"reason": "x"})).json()
    principals = {
        role: (await add_principal(api, tenant, roles=[(role, "matter", uuid.UUID(m))]))[1]
        for role in ("matter_manager", "reviewer", "auditor", "collector")
    }
    for role, subject in principals.items():
        async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
            listed = [h["id"] for h in (await c.get("/v1/legal-holds")).json()["items"]]
            got = (await c.get(f"/v1/legal-holds/{hold['id']}")).status_code
            readable = role in ("matter_manager", "auditor")
            assert (got == 200) is readable, (role, got)
            if readable:  # every hold on the matter, and the list agrees with GET for each
                assert hold["id"] in listed, role
                for h in listed:
                    assert (await c.get(f"/v1/legal-holds/{h}")).status_code == 200, (role, h)
            else:
                assert listed == [], role
            r = await c.post(f"/v1/matters/{m}/legal-holds", json={"reason": "again"})
            assert (r.status_code == 201) is (role == "matter_manager"), (role, r.status_code)
            # the release is a tenant admin's only (D3)
            r = await c.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "y"})
            assert r.status_code in (403, 404), (role, r.status_code)
            # a matter they have no role at
            r = await c.post(f"/v1/matters/{m_other}/legal-holds", json={"reason": "x"})
            assert r.status_code == 404, (role, r.status_code)
    async with api.client(other_tenant.subdomain, other_tenant.token(api.settings)) as o:
        assert (await o.get(f"/v1/legal-holds/{hold['id']}")).status_code == 404
        assert (
            await o.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "y"})
        ).status_code == 404
        assert (
            await o.post(f"/v1/matters/{m}/legal-holds", json={"reason": "x"})
        ).status_code == 404
        assert (await o.get("/v1/legal-holds")).json()["items"] == []


async def test_the_cooling_off_is_a_tenant_setting_bounded_7_to_90_days(
    api: Api, tenant: TenantCtx
) -> None:
    second = await _second_admin(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as a:
        for days in (6, 91):
            r = await a.put(
                "/v1/tenant/settings/hold-cooling-off", json={"days": days, "reason": "x"}
            )
            assert r.status_code == 422, (days, r.text)
        r = await a.put(
            "/v1/tenant/settings/hold-cooling-off", json={"days": 30, "reason": "policy"}
        )
        assert r.status_code == 200 and r.json()["hold_cooling_off_days"] == 30
        m = await _matter(a, tenant.default_client_id)
        hold = (await a.post(f"/v1/matters/{m}/legal-holds", json={"reason": "x"})).json()
        assert (
            await a.post(f"/v1/legal-holds/{hold['id']}/release", json={"reason": "y"})
        ).status_code == 200
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=second)) as b:
        assert (await b.post(f"/v1/legal-holds/{hold['id']}/release/approve")).status_code == 200
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        row = (await s.execute(text(
            "SELECT released_at, cooling_off_until FROM legal_holds WHERE id = :h"),
            {"h": uuid.UUID(hold["id"])})).one()  # fmt: skip
    assert row.cooling_off_until - row.released_at == timedelta(days=30)
    changes = list(await _events(api, tenant, "tenant_setting_changed"))
    assert changes[-1]["setting"] == "hold_cooling_off_days" and changes[-1]["new"] == 30
