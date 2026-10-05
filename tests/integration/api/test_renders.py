"""Renders API (M15 step 4, ADR 0015 §14): who may create renders, idempotent creation, refusals,
status/files/custody reads, audited downloads, and production files kept out of the generic
evidence content endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from temporalio.worker import Worker

from edisc_connector_dummy.connector import DummyConnector
from edisc_db.session import tenant_tx
from edisc_worker.activities import Activities
from edisc_worker.contracts import task_queue
from edisc_worker.renders import RenderActivities
from edisc_worker.workflows import RenderWorkflow

from ..custody.conftest import superuser
from ..worker.conftest import WORKFLOWS
from .conftest import Api, TenantCtx, add_principal
from .test_jobs import World, custody, make_world, wait_status


@pytest.fixture
async def render_worker(api: Api) -> AsyncIterator[None]:
    """The collection worker (collect-dummy) plus the renders worker, on the queues the API uses."""
    collect = Activities(
        api.sessions,
        api.s3,
        api.settings.model_copy(update={"activity_time_box_seconds": 30}),
        {"dummy": DummyConnector(api.resources.limiter)},
        api.temporal,
    )
    settings = api.settings.model_copy(update={"render_files_batch_size": 2})
    rendering = RenderActivities(
        api.sessions, api.s3, settings
    )  # its queue: this runtime's versions
    async with (
        Worker(
            api.temporal, task_queue=task_queue("dummy"), workflows=WORKFLOWS,
            activities=collect.all(),
        ),
        Worker(
            api.temporal, task_queue=rendering.task_queue, workflows=[RenderWorkflow],
            activities=rendering.all(),
        ),
    ):  # fmt: skip
        yield


async def sealed_job(api: Api, t: TenantCtx) -> tuple[World, str]:
    w = await make_world(api, t)
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    job = await wait_status(api, t, job_id)
    assert job["sealed"], job
    return w, job_id


async def wait_render(c: httpx.AsyncClient, render_id: str, within: float = 90) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + within
    while True:
        r = (await c.get(f"/v1/renders/{render_id}")).json()
        if r["sealed"] or asyncio.get_running_loop().time() > deadline:
            return r
        await asyncio.sleep(0.3)


async def _renders(api: Api, t: TenantCtx, job_id: str) -> int:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return int(
            (
                await s.execute(
                    text("SELECT count(*) FROM renders WHERE job_id = :j"), {"j": uuid.UUID(job_id)}
                )
            ).scalar_one()
        )


# ------------------------------------------------------------------ who may do what
async def test_only_matter_managers_and_tenant_admins_create_and_download(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    w, job_id = await sealed_job(api, tenant)
    matter, client = uuid.UUID(w.matter), uuid.UUID(w.client)
    subjects = {
        role: (await add_principal(api, tenant, roles=[(role, scope, sid)]))[1]
        for role, scope, sid in [
            ("matter_manager", "matter", matter),
            ("reviewer", "matter", matter),
            ("collector", "matter", matter),
            ("auditor", "matter", matter),
            ("client_admin", "client", client),
        ]
    }
    other = await make_world(api, tenant)
    _, outsider = await add_principal(
        api, tenant, roles=[("matter_manager", "matter", uuid.UUID(other.matter))]
    )

    def as_(subject: str | None) -> httpx.AsyncClient:
        return api.client(tenant.subdomain, tenant.token(api.settings, subject=subject))

    for role in ("reviewer", "collector", "auditor", "client_admin"):
        async with as_(subjects[role]) as c:
            r = await c.post(f"/v1/jobs/{job_id}/renders", json={})
            assert r.status_code == 403, (role, r.text)
    async with as_(outsider) as c:
        assert (await c.post(f"/v1/jobs/{job_id}/renders", json={})).status_code == 404
    assert await _renders(api, tenant, job_id) == 0

    async with as_(None) as c:  # the tenant admin
        created = await c.post(f"/v1/jobs/{job_id}/renders", json={})
        assert created.status_code == 201, created.text
    async with as_(subjects["matter_manager"]) as c:
        same = await c.post(f"/v1/jobs/{job_id}/renders", json={})
        assert (same.status_code, same.json()["id"]) == (
            200,
            created.json()["id"],
        )  # the live render
        render = await wait_render(c, created.json()["id"])
        assert (render["status"], render["sealed"]) == ("completed", True), render
        assert (
            render["job_seal_key"] and render["job_head_seq"] and render["summary"]["items_in"] > 0
        )
        files, cursor = [], None
        while True:  # paginated
            page = (
                await c.get(
                    f"/v1/renders/{render['id']}/files",
                    params={"limit": 2, **({"cursor": cursor} if cursor else {})},
                )
            ).json()
            files += page["items"]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert [f["ord"] for f in files] == list(range(render["file_count"]))
        verify = (await c.get(f"/v1/renders/{render['id']}/custody/verify")).json()
        assert verify["ok"] and verify["files_checked"] == render["file_count"], verify
        got = await c.get(f"/v1/renders/{render['id']}/files/0/content")
        assert got.status_code == 200
        assert (
            hashlib.sha256(got.content).hexdigest()
            == files[0]["sha256"]
            == got.headers["x-evidence-sha256"]
        )
        assert got.headers["content-disposition"] == f'attachment; filename="{files[0]["name"]}"'
        listed = (await c.get(f"/v1/jobs/{job_id}/renders")).json()["items"]
        assert [r["id"] for r in listed] == [render["id"]]

    # auditors see status, files and custody, never bytes; reviewers see none of it
    async with as_(subjects["auditor"]) as c:
        assert (await c.get(f"/v1/renders/{render['id']}")).status_code == 200
        assert (await c.get(f"/v1/renders/{render['id']}/files")).status_code == 200
        assert (await c.get(f"/v1/renders/{render['id']}/files/0/content")).status_code == 403
    async with as_(subjects["reviewer"]) as c:
        assert (await c.get(f"/v1/renders/{render['id']}/files/0/content")).status_code == 403
        assert (await c.get(f"/v1/renders/{render['id']}")).status_code == 403

    reads = [
        e
        for e in await custody(api, tenant, str(tenant.tenant_id))
        if e.event_type == "audit.render_file_read"
    ]
    assert len(reads) == 1  # refused downloads are not reads
    assert reads[0].payload["sha256"] == files[0]["sha256"]
    assert reads[0].payload["purpose"] == "rsmf" and reads[0].actor.startswith("user:")
    requested = [
        e.payload["created"]
        for e in await custody(api, tenant, str(tenant.tenant_id))
        if e.event_type == "audit.render_requested"
    ]
    assert requested == [True, False]


async def test_production_files_are_not_served_by_the_generic_evidence_endpoint(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    w, job_id = await sealed_job(api, tenant)
    _, reviewer = await add_principal(
        api, tenant, roles=[("reviewer", "matter", uuid.UUID(w.matter))]
    )
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        render = await wait_render(
            c, (await c.post(f"/v1/jobs/{job_id}/renders", json={})).json()["id"]
        )
        production = (await c.get(f"/v1/renders/{render['id']}/files")).json()["items"][0]
        for purpose in ("rsmf", "download"):  # not even a tenant admin, whatever the purpose
            r = await c.get(
                f"/v1/evidence/{production['evidence_id']}/content", params={"purpose": purpose}
            )
            assert r.status_code == 404
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=reviewer)) as c:
        r = await c.get(
            f"/v1/evidence/{production['evidence_id']}/content", params={"purpose": "preview"}
        )
        assert r.status_code == 404  # a reviewer holds evidence.read, and is still refused
    reads = [
        e
        for e in await custody(api, tenant, str(tenant.tenant_id))
        if e.event_type == "audit.evidence_content_read"
    ]
    assert reads == []


# ------------------------------------------------------------------ idempotency
async def test_twenty_concurrent_identical_requests_create_one_render(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, job_id = await sealed_job(api, tenant)
    key = f"render-{uuid.uuid4()}"
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        keyed = await asyncio.gather(
            *(
                c.post(f"/v1/jobs/{job_id}/renders", json={}, headers={"Idempotency-Key": key})
                for _ in range(20)
            )
        )
        assert {r.status_code for r in keyed} == {201}
        ids = {r.json()["id"] for r in keyed}
        assert len(ids) == 1
        render_id = ids.pop()
        unkeyed = await asyncio.gather(
            *(c.post(f"/v1/jobs/{job_id}/renders", json={}) for _ in range(20))
        )
        assert {r.status_code for r in unkeyed} == {200}
        assert {r.json()["id"] for r in unkeyed} == {render_id}
        mismatch = await c.post(
            f"/v1/jobs/{job_id}/renders",
            json={"include_context": False},
            headers={"Idempotency-Key": key},
        )
        assert mismatch.status_code == 422
        other = await c.post(f"/v1/jobs/{job_id}/renders", json={"time_zone": "America/New_York"})
        assert other.status_code == 201 and other.json()["id"] != render_id
        bad_zone = await c.post(f"/v1/jobs/{job_id}/renders", json={"time_zone": "Mars/Base"})
        assert bad_zone.status_code == 422
        for rid in (render_id, other.json()["id"]):
            assert (await wait_render(c, rid))["status"] == "completed"
    assert await _renders(api, tenant, job_id) == 2
    runs = [wf async for wf in api.temporal.list_workflows(f"WorkflowId = 'render-{render_id}'")]
    assert len(runs) <= 1  # visibility may lag; the id itself can never be started twice
    requested = [
        e
        for e in await custody(api, tenant, str(tenant.tenant_id))
        if e.event_type == "audit.render_requested" and e.payload["render_id"] == render_id
    ]
    assert sum(e.payload["created"] for e in requested) == 1
    assert len(requested) == 21  # one keyed request (replays are the same request) + 20 unkeyed


# ------------------------------------------------------------------ refusals
async def test_unsealed_jobs_and_closed_matters_or_clients_are_refused(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    w, job_id = await sealed_job(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        conn = await superuser(api.settings)
        try:
            await conn.execute(
                "UPDATE collection_jobs SET sealed_at = NULL WHERE id = $1", uuid.UUID(job_id)
            )
            r = await c.post(f"/v1/jobs/{job_id}/renders", json={})
            assert (r.status_code, r.json()["error"]) == (409, "job_not_sealed")
            await conn.execute(
                "UPDATE collection_jobs SET sealed_at = finished_at WHERE id = $1",
                uuid.UUID(job_id),
            )
        finally:
            await conn.close()
        assert (await c.post(f"/v1/matters/{w.matter}/close")).status_code == 200
        r = await c.post(f"/v1/jobs/{job_id}/renders", json={})
        assert (r.status_code, r.json()["error"]) == (409, "matter_closed")
        assert (await c.post(f"/v1/matters/{w.matter}/reopen")).status_code == 200
        conn = await superuser(api.settings)
        try:  # (closing a client through the API needs every matter closed first)
            await conn.execute(
                "UPDATE clients SET closed_at = now(), closed_by = 'tests' WHERE id = $1",
                uuid.UUID(w.client),
            )
        finally:
            await conn.close()
        r = await c.post(f"/v1/jobs/{job_id}/renders", json={})
        assert (r.status_code, r.json()["error"]) == (409, "client_closed")
    assert await _renders(api, tenant, job_id) == 0  # nothing was created


async def test_a_refused_render_is_recorded_and_does_not_block_a_new_request(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, job_id = await sealed_job(api, tenant)
    conn = await superuser(api.settings)
    try:  # the sealed job's chain no longer verifies
        await conn.execute(
            "UPDATE custody_events SET actor = 'mallory' WHERE stream_id = $1 AND seq = 2",
            uuid.UUID(job_id),
        )
    finally:
        await conn.close()
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        first = (await c.post(f"/v1/jobs/{job_id}/renders", json={})).json()
        refused = await wait_render(c, first["id"])
        assert (refused["status"], refused["reason"]) == ("refused", "chain_verification_failed")
        assert refused["file_count"] is None and refused["sealed"]
        again = await c.post(f"/v1/jobs/{job_id}/renders", json={})
        assert again.status_code == 201 and again.json()["id"] != first["id"]
        assert (await wait_render(c, again.json()["id"]))["status"] == "refused"
        assert (await c.get(f"/v1/renders/{first['id']}/files/0/content")).status_code == 409
