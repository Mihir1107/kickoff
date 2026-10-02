"""Closing matters and clients (ADR 0002 amendment): irreversible, audited, and closed owners take no
new work (jobs, matters, exports)."""

from __future__ import annotations

from sqlalchemy import text

from edisc_db.session import tenant_tx

from .conftest import Api, TenantCtx, add_principal
from .test_jobs import make_world


async def test_close_matter_then_client(api: Api, tenant: TenantCtx) -> None:
    w = await make_world(api, tenant)
    _, client_admin = await add_principal(api, tenant, roles=[("client_admin", "client", w.client)])
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        r = await c.post(f"/v1/clients/{w.client}/close")
        assert r.status_code == 409 and "still open" in r.json()["detail"]
        r = await c.post(f"/v1/matters/{w.matter}/close")
        assert r.status_code == 200 and r.json()["closed_at"]
        assert (await c.post(f"/v1/matters/{w.matter}/close")).status_code == 409
        r = await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())
        assert r.status_code == 409 and r.json()["detail"] == "the matter is closed"
        assert (await c.post(f"/v1/clients/{tenant.default_client_id}/close")).status_code == 409
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=client_admin)) as c:
        assert (await c.post(f"/v1/clients/{w.client}/close")).status_code == 403
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        r = await c.post(f"/v1/clients/{w.client}/close")
        assert r.status_code == 200 and r.json()["closed_at"]
        r = await c.post(
            f"/v1/clients/{w.client}/matters",
            json={"name": "late", "retention_until": "2099-01-01T00:00:00Z"},
        )
        assert r.status_code == 409
        r = await c.post(f"/v1/clients/{w.client}/exports", json={"size_bytes": 10})
        assert r.status_code == 409
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        events = (
            await s.execute(
                text(
                    "SELECT event_type, actor FROM custody_events WHERE stream_id = :t"
                    " AND event_type IN ('audit.matter_closed', 'audit.client_closed') ORDER BY seq"
                ),
                {"t": tenant.tenant_id},
            )
        ).all()
    assert [e.event_type for e in events] == ["audit.matter_closed", "audit.client_closed"]
    assert {e.actor for e in events} == {f"user:{tenant.admin_id}"}
