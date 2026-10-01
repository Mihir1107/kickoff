"""Jobs API (M13.6): idempotent creation, actions with the acting user in custody, status that never
presents an unverified result as clean, units/reconciliation/custody reads, audited evidence content."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.worker import Worker

from edisc_connector_dummy.connector import DummyConnector
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_core.time import utc_now
from edisc_db.session import tenant_tx
from edisc_worker.activities import Activities
from edisc_worker.contracts import task_queue

from ..worker.conftest import WORKFLOWS, spec
from .conftest import Api, TenantCtx, add_principal, secret


@pytest.fixture
async def worker(api: Api) -> AsyncIterator[None]:
    """The collection worker on the queue the API starts jobs on (collect-dummy)."""
    acts = Activities(
        api.sessions,
        api.s3,
        api.settings.model_copy(update={"activity_time_box_seconds": 30}),
        {"dummy": DummyConnector(api.resources.limiter)},
        api.temporal,
    )
    async with Worker(
        api.temporal, task_queue=task_queue("dummy"), workflows=WORKFLOWS, activities=acts.all()
    ):
        yield


class World:
    def __init__(self, client: str, matter: str, connection: str, sp: DatasetSpec) -> None:
        self.client, self.matter, self.connection, self.sp = client, matter, connection, sp

    def job_body(self, **overrides: Any) -> dict[str, Any]:
        ds = Dataset(self.sp)
        start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
        body = {
            "connection_id": self.connection,
            "scopes": [
                {
                    "type": "channel",
                    "external_id": "*",
                    "date_from": start.isoformat(),
                    "date_to": (start + timedelta(days=ds.n_days(0))).isoformat(),
                }
            ],
        }
        return {**body, **overrides}


async def make_world(api: Api, t: TenantCtx, sp: DatasetSpec | None = None) -> World:
    sp = sp or spec()
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        client = (await c.post("/v1/clients", json={"name": "Client"})).json()["id"]
        matter = (
            await c.post(
                f"/v1/clients/{client}/matters",
                json={
                    "name": "Matter",
                    "retention_until": (utc_now() + timedelta(days=30)).isoformat(),
                },
            )
        ).json()["id"]
        conn = (
            await c.post(
                f"/v1/clients/{client}/connections",
                json={
                    "source": "dummy",
                    "external_org_id": sp.workspace_id,
                    "config": {"spec": sp.model_dump(mode="json"), "epoch": 0},
                    "credentials": {"access_token": secret()},
                },
            )
        ).json()["id"]
    return World(client, matter, conn, sp)


async def wait_status(api: Api, t: TenantCtx, job_id: str, *, within: float = 90) -> dict[str, Any]:
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        deadline = asyncio.get_running_loop().time() + within
        while True:
            job = (await c.get(f"/v1/jobs/{job_id}")).json()
            if job["sealed"] or asyncio.get_running_loop().time() > deadline:
                return job
            await asyncio.sleep(0.3)


async def custody(api: Api, t: TenantCtx, stream: str) -> list[Any]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT event_type, actor, payload FROM custody_events WHERE stream_id = :s ORDER BY seq"
                    ),
                    {"s": uuid.UUID(stream)},
                )
            ).all()
        )


async def test_concurrent_identical_posts_with_one_key_start_exactly_one_job(
    api: Api, tenant: TenantCtx, worker: None
) -> None:
    w = await make_world(api, tenant)
    key = f"create-{uuid.uuid4()}"
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        responses = await asyncio.gather(
            *(
                c.post(
                    f"/v1/matters/{w.matter}/jobs",
                    json=w.job_body(),
                    headers={"Idempotency-Key": key},
                )
                for _ in range(20)
            )
        )
        assert {r.status_code for r in responses} == {201}
        ids = {r.json()["id"] for r in responses}
        assert len(ids) == 1
        job_id = ids.pop()
        # a retry after a lost response returns the same job
        again = await c.post(
            f"/v1/matters/{w.matter}/jobs", json=w.job_body(), headers={"Idempotency-Key": key}
        )
        assert (again.status_code, again.json()["id"]) == (201, job_id)
        # the same key with a different request is refused
        other = w.job_body()
        other["scopes"][0]["thread_parent_policy"] = "replies_only"
        mismatch = await c.post(
            f"/v1/matters/{w.matter}/jobs", json=other, headers={"Idempotency-Key": key}
        )
        assert mismatch.status_code == 422
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        jobs = (
            await s.execute(
                text("SELECT count(*) FROM collection_jobs WHERE matter_id = :m"),
                {"m": uuid.UUID(w.matter)},
            )
        ).scalar_one()
    assert jobs == 1
    runs = [wf async for wf in api.temporal.list_workflows(f"WorkflowId = '{job_id}'")]
    assert len(runs) <= 1  # visibility may lag; the id itself can never be started twice
    job = await wait_status(api, tenant, job_id)
    assert (job["status"], job["clean"]) == ("completed", True)
    events = await custody(api, tenant, job_id)
    started = events[0]
    assert (started.event_type, started.actor) == ("job_started", f"user:{tenant.admin_id}")
    assert started.payload["idempotency_key"] == key and "request_id" in started.payload


async def test_posts_without_a_key_are_separate_jobs(
    api: Api, tenant: TenantCtx, worker: None
) -> None:
    w = await make_world(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        a = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
        b = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    assert a != b
    for j in (a, b):
        assert (await wait_status(api, tenant, j))["status"] == "completed"


async def test_job_reads_units_reconciliation_and_custody(
    api: Api, tenant: TenantCtx, worker: None
) -> None:
    w = await make_world(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
        await wait_status(api, tenant, job_id)
        seen, cursor = [], None
        while True:
            page = (
                await c.get(
                    f"/v1/jobs/{job_id}/units",
                    params={"limit": 2, **({"cursor": cursor} if cursor else {})},
                )
            ).json()
            seen += [u["unit_key"] for u in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        recon = (await c.get(f"/v1/jobs/{job_id}/reconciliation")).json()
        verify = (await c.get(f"/v1/jobs/{job_id}/custody/verify")).json()
        jobs = (await c.get(f"/v1/matters/{w.matter}/jobs")).json()["items"]
    assert len(seen) == len(set(seen)) == sum(1 for _ in seen)
    assert (
        recon["clean"]
        and set(recon["by_recon_status"]) == {"matched"}
        and recon["not_matched"] == []
    )
    assert verify["ok"] and verify["items_checked"] > 0
    assert [j["id"] for j in jobs] == [job_id]


async def test_unverifiable_results_are_never_presented_as_clean(
    api: Api, tenant: TenantCtx, worker: None
) -> None:
    w = await make_world(api, tenant, spec(count_mode="unavailable"))
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    job = await wait_status(api, tenant, job_id)
    assert (job["status"], job["clean"]) == ("completed_unverified", False)


async def test_cancel_and_resume_record_the_acting_user(
    api: Api, tenant: TenantCtx, worker: None
) -> None:
    w = await make_world(api, tenant, spec(conversations=4, messages_per_unit=40))
    _, subject = await add_principal(
        api, tenant, roles=[("matter_manager", "matter", uuid.UUID(w.matter))]
    )
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
        assert (await c.post(f"/v1/jobs/{job_id}/resume")).status_code == 200
        cancelled = await c.post(f"/v1/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
    job = await wait_status(api, tenant, job_id)
    assert job["status"] == "cancelled"
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        assert (await c.post(f"/v1/jobs/{job_id}/cancel")).status_code == 409
        assert (await c.post(f"/v1/jobs/{job_id}/resume")).status_code == 409
    events = {e.event_type: e.actor for e in await custody(api, tenant, job_id)}
    me = next(e.actor for e in await custody(api, tenant, job_id) if e.event_type == "job_started")
    assert events["resume_requested"] == me and events["cancel_requested"] == me
    assert me.startswith("user:")


async def test_rerun_of_failed_units_is_a_new_job_with_idempotency(
    api: Api, tenant: TenantCtx, worker: None
) -> None:
    broken = spec(failures={"corrupt_conversations": [1]})
    w = await make_world(api, tenant, broken)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
        job = await wait_status(api, tenant, job_id)
        assert (job["status"], job["clean"]) == ("completed_with_failed_units", False)
        # fixed at the source
        async with tenant_tx(api.sessions, tenant.tenant_id) as s:
            await s.execute(
                text(
                    "UPDATE connections SET config = jsonb_set(config, '{spec,failures,corrupt_conversations}', '[]') WHERE id = :i"
                ),
                {"i": uuid.UUID(w.connection)},
            )
        key = f"rerun-{uuid.uuid4()}"
        first = await c.post(f"/v1/jobs/{job_id}/rerun", headers={"Idempotency-Key": key})
        second = await c.post(f"/v1/jobs/{job_id}/rerun", headers={"Idempotency-Key": key})
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"] and first.json()["rerun_of"] == job_id
    rerun = await wait_status(api, tenant, first.json()["id"])
    assert rerun["status"] == "completed"
    events = await custody(api, tenant, rerun["id"])
    assert any(
        e.event_type == "rerun_requested" and e.actor == f"user:{tenant.admin_id}" for e in events
    )


async def test_evidence_content_is_audited_before_it_is_returned(
    api: Api, tenant: TenantCtx, worker: None
) -> None:
    w = await make_world(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    await wait_status(api, tenant, job_id)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        evidence = (
            await s.execute(
                text(
                    "SELECT id, sha256 FROM evidence_objects WHERE job_id = :j AND kind = 'page' AND state = 'complete' LIMIT 1"
                ),
                {"j": uuid.UUID(job_id)},
            )
        ).one()
    reviewer, r_sub = await add_principal(
        api, tenant, roles=[("reviewer", "matter", uuid.UUID(w.matter))]
    )
    _, c_sub = await add_principal(
        api, tenant, roles=[("collector", "matter", uuid.UUID(w.matter))]
    )
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=r_sub)) as c:
        r = await c.get(f"/v1/evidence/{evidence.id}/content", params={"purpose": "preview"})
        assert r.status_code == 200
        assert (
            hashlib.sha256(r.content).hexdigest()
            == evidence.sha256
            == r.headers["x-evidence-sha256"]
        )
        assert (
            await c.get(f"/v1/evidence/{evidence.id}/content")
        ).status_code == 422  # purpose required
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=c_sub)) as c:
        assert (
            await c.get(f"/v1/evidence/{evidence.id}/content", params={"purpose": "download"})
        ).status_code == 403
    reads = [
        e
        for e in await custody(api, tenant, str(tenant.tenant_id))
        if e.event_type == "audit.evidence_content_read"
    ]
    assert len(reads) == 1  # the denied read is not a read
    assert reads[0].actor == f"user:{reviewer}"
    assert (
        reads[0].payload["purpose"] == "preview" and reads[0].payload["sha256"] == evidence.sha256
    )


async def test_jobs_are_invisible_outside_the_callers_matter(
    api: Api, tenant: TenantCtx, worker: None
) -> None:
    w = await make_world(api, tenant)
    other = await make_world(api, tenant)
    _, subject = await add_principal(
        api, tenant, roles=[("matter_manager", "matter", uuid.UUID(other.matter))]
    )
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
        for method, path in [("get", f"/v1/jobs/{job_id}"), ("post", f"/v1/jobs/{job_id}/cancel"),
                             ("get", f"/v1/jobs/{job_id}/units"), ("get", f"/v1/jobs/{job_id}/custody/verify")]:  # fmt: skip
            assert (await getattr(c, method)(path)).status_code == 404, path
        # a connection of another client cannot be used for this matter
        bad = await c.post(f"/v1/matters/{other.matter}/jobs", json=w.job_body())
        assert bad.status_code == 422
    await wait_status(api, tenant, job_id)
