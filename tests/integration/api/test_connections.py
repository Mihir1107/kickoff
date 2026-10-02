"""Client-owned connections (ADR 0013 decision a): only connection.manage creates, re-authorizes or
disables; matters use their client's connections; credentials never leave the token store."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.worker import Worker

from edisc_core.settings import Environment
from edisc_core.time import utc_now
from edisc_db.session import tenant_tx

from ..worker.conftest import FAST, WORKFLOWS, queue, spec
from .conftest import SECRETS, Api, TenantCtx, add_principal, secret
from .test_jobs import make_world


async def _client_and_matter(api: Api, t: TenantCtx) -> tuple[str, str]:
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        client = (await c.post("/v1/clients", json={"name": "Acme Corp"})).json()["id"]
        matter = (
            await c.post(
                f"/v1/clients/{client}/matters",
                json={
                    "name": "Acme v. Doe",
                    "retention_until": (utc_now() + timedelta(days=30)).isoformat(),
                },
            )
        ).json()["id"]
    return client, matter


def _connection_body(sp: Any = None, **config: Any) -> dict[str, Any]:
    sp = sp or spec()
    return {
        "source": "dummy",
        "external_org_id": sp.workspace_id,
        "config": {"spec": sp.model_dump(mode="json"), "epoch": 0, **config},
        "credentials": {"access_token": secret(), "refresh_token": secret("xoxe")},
    }


async def test_client_admin_creates_a_connection_and_no_credential_is_ever_returned(
    api: Api, tenant: TenantCtx, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = await _client_and_matter(api, tenant)
    _, subject = await add_principal(api, tenant, roles=[("client_admin", "client", client)])
    body = _connection_body()
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        r = await c.post(f"/v1/clients/{client}/connections", json=body)
        assert r.status_code == 201, r.text
        conn = r.json()
        assert conn["status"] == "active" and conn["client_id"] == client
        assert set(conn) == {
            "id", "client_id", "source", "external_org_id", "status", "plan_tier",
            "granted_scopes", "created_at", "updated_at",
        }  # fmt: skip
        listed = (await c.get(f"/v1/clients/{client}/connections")).json()
        assert [x["id"] for x in listed["items"]] == [conn["id"]]
        assert (await c.get(f"/v1/connections/{conn['id']}")).status_code == 200
    token = body["credentials"]["access_token"]
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        stored = (
            await s.execute(
                text("SELECT encrypted_access_token, token_version FROM connections WHERE id = :i"),
                {"i": conn["id"]},
            )
        ).one()
        payloads = [
            str(p)
            for p in (
                await s.execute(
                    text("SELECT payload FROM custody_events WHERE stream_id = :t"),
                    {"t": tenant.tenant_id},
                )
            ).scalars()
        ]
    assert stored.token_version == 1 and token.encode() not in stored.encrypted_access_token
    assert not any(token in p for p in payloads)  # audit names the connection, never the credential
    assert not any(s in caplog.text for s in SECRETS)


async def test_matter_roles_use_but_cannot_manage_the_clients_connections(
    api: Api, tenant: TenantCtx
) -> None:
    client, matter = await _client_and_matter(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        conn = (await c.post(f"/v1/clients/{client}/connections", json=_connection_body())).json()
    _, subject = await add_principal(api, tenant, roles=[("collector", "matter", matter)])
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        usable = (await c.get(f"/v1/matters/{matter}/connections")).json()["items"]
        assert [x["id"] for x in usable] == [conn["id"]]
        assert (
            await c.post(f"/v1/clients/{client}/connections", json=_connection_body())
        ).status_code == 404
        reauth = await c.post(
            f"/v1/connections/{conn['id']}/reauth", json={"credentials": {"access_token": secret()}}
        )
        assert reauth.status_code == 404  # the client (owner) is not visible to a matter role
        assert (await c.post(f"/v1/connections/{conn['id']}/disable")).status_code == 404


async def test_reauthorization_resumes_paused_jobs_with_the_acting_user_in_custody(
    api: Api, tenant: TenantCtx
) -> None:
    from edisc_connector_dummy.connector import DummyConnector, scope_for_days
    from edisc_connector_dummy.dataset import Dataset
    from edisc_core.ids import new_id
    from edisc_worker.activities import Activities
    from edisc_worker.contracts import JobInput
    from edisc_worker.pipeline import Pipeline
    from edisc_worker.workflows import CollectionJobWorkflow

    sp = spec()
    client, matter = await _client_and_matter(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        conn = (
            await c.post(
                f"/v1/clients/{client}/connections", json=_connection_body(sp, auth_revoked=True)
            )
        ).json()
    # a job on the connection (job creation through the API arrives in M13.6)
    ds, job_id = Dataset(sp), new_id()
    connector = DummyConnector(api.resources.limiter)
    await Pipeline(api.sessions, api.s3, api.settings, connector).start_job(
        tenant_id=tenant.tenant_id, job_id=job_id, matter_id=matter, connection_id=conn["id"],
        scopes=[scope_for_days("*", datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC), ds.n_days(0))],
        requested_by=f"user:{tenant.admin_id}",
    )  # fmt: skip
    q = queue()
    acts = Activities(
        api.sessions,
        api.s3,
        api.settings.model_copy(update={"activity_time_box_seconds": 30}),
        {"dummy": connector},
        api.temporal,
    )
    async with Worker(api.temporal, task_queue=q, workflows=WORKFLOWS, activities=acts.all()):
        handle = await api.temporal.start_workflow(
            CollectionJobWorkflow.run,
            JobInput(str(tenant.tenant_id), str(job_id), FAST),
            id=str(job_id),
            task_queue=q,
        )
        for _ in range(100):
            async with tenant_tx(api.sessions, tenant.tenant_id) as s:
                status = (
                    await s.execute(
                        text("SELECT status FROM collection_jobs WHERE id = :j"), {"j": job_id}
                    )
                ).scalar_one()
            if status == "paused_awaiting_reauth":
                break
            await asyncio.sleep(0.2)
        assert status == "paused_awaiting_reauth"
        # the source accepts the new credentials (the dummy reads its revocation flag from config)
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            await s.execute(
                text("UPDATE connections SET config = config - 'auth_revoked' WHERE id = :i"),
                {"i": conn["id"]},
            )
        async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
            r = await c.post(
                f"/v1/connections/{conn['id']}/reauth",
                json={"credentials": {"access_token": secret()}},
            )
        assert r.status_code == 200 and r.json()["status"] == "active"
        assert await handle.result() == "completed"
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        resumed = (
            (
                await s.execute(
                    text(
                        "SELECT actor FROM custody_events WHERE stream_id = :j AND event_type = 'job_resumed'"
                    ),
                    {"j": job_id},
                )
            )
            .scalars()
            .all()
        )
        audit = (
            await s.execute(
                text(
                    "SELECT payload FROM custody_events WHERE stream_id = :t AND event_type = 'audit.connection_reauthorized'"
                ),
                {"t": tenant.tenant_id},
            )
        ).scalar_one()
    assert resumed == [f"user:{tenant.admin_id}"]
    assert audit["resumed_jobs"] == [str(job_id)]


async def test_a_role_that_sees_the_client_but_cannot_manage_gets_403(
    api: Api, tenant: TenantCtx
) -> None:
    client, _ = await _client_and_matter(api, tenant)
    _, subject = await add_principal(api, tenant, roles=[("auditor", "client", client)])
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        r = await c.post(f"/v1/clients/{client}/connections", json=_connection_body())
    assert r.status_code == 403


async def test_the_response_scanner_itself_catches_a_leak() -> None:
    """The suite-wide secret scan must not be vacuous."""
    import httpx

    from .conftest import _scan_response

    canary = secret()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=f'{{"token": "{canary}"}}'))
    async with httpx.AsyncClient(
        transport=transport, event_hooks={"response": [_scan_response]}
    ) as c:
        with pytest.raises(AssertionError, match="leaked a credential"):
            await c.get("http://x/")


@pytest.mark.parametrize("env", [Environment.STAGING, Environment.PRODUCTION])
async def test_the_dummy_connector_is_refused_outside_disposable_environments(
    api: Api, tenant: TenantCtx, env: Environment
) -> None:
    """The dummy shares the slack identity namespace (ADR 0004): outside local/test/ci it gets no
    connection and starts no job, even with a connector wired and a connection left from before."""
    w = await make_world(api, tenant)  # created while the stack is a test stack
    real = api.resources.settings
    api.resources.settings = real.model_copy(update={"env": env})
    try:
        async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
            r = await c.post(f"/v1/clients/{w.client}/connections", json=_connection_body())
            assert (r.status_code, r.json()["detail"]) == (422, "unknown source dummy")
            r = await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())
            assert (r.status_code, r.json()["detail"]) == (422, "no connector for dummy")
    finally:
        api.resources.settings = real
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        counts = (
            await s.execute(
                text(
                    "SELECT (SELECT count(*) FROM connections), (SELECT count(*) FROM collection_jobs)"
                )
            )
        ).one()
    assert tuple(counts) == (1, 0)
