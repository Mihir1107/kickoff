"""Every endpoint that serves evidence content commits its audit event AND anchors it before the first
byte (ADR 0013 decision d, ADR 0015 §14.7 and §19). Read at the moment the first body byte is sent.

The endpoints: ``/v1/evidence/{id}/content`` (job evidence: pages, files and export archive entries),
``/v1/renders/{id}/files/{ord}/content``, ``/v1/renders/{id}/package`` (in
``test_render_packages.py``) and ``/v1/renders/{id}/natives/{sha256}/content`` (in
``test_render_natives.py``). No other route returns evidence bytes."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from edisc_db.session import tenant_tx

from .conftest import Api, TenantCtx
from .cut import cut_every_way, full_length
from .first_byte import first_byte
from .test_renders import render_worker, sealed_job, wait_render

__all__ = ["render_worker"]


async def test_evidence_content_read_is_anchored_before_the_first_byte(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, job_id = await sealed_job(api, tenant)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        evidence_id = (
            await s.execute(
                text(
                    "SELECT id FROM evidence_objects WHERE job_id = :j AND kind = 'page'"
                    " AND state = 'complete' ORDER BY id LIMIT 1"
                ),
                {"j": uuid.UUID(job_id)},
            )
        ).scalar_one()
    got = await first_byte(api, tenant, f"/v1/evidence/{evidence_id}/content", "purpose=preview")
    assert got.status == 200
    assert got.newest.event_type == "audit.evidence_content_read", got.newest
    assert got.newest.payload["evidence_id"] == str(evidence_id)
    assert got.anchored >= got.newest.seq


async def test_render_file_read_is_anchored_before_the_first_byte(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, job_id = await sealed_job(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        render = await wait_render(
            c, (await c.post(f"/v1/jobs/{job_id}/renders", json={})).json()["id"]
        )
    assert render["status"] == "completed", render
    got = await first_byte(api, tenant, f"/v1/renders/{render['id']}/files/0/content")
    assert got.status == 200
    assert got.newest.event_type == "audit.render_file_read", got.newest
    assert got.newest.payload["ord"] == 0
    assert got.anchored >= got.newest.seq


# ------------------------------------------------------------------ cut when the session ends (5b)
async def test_evidence_content_is_cut_when_the_session_is_revoked(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, job_id = await sealed_job(api, tenant)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        evidence_id = (await s.execute(text(
            "SELECT id FROM evidence_objects WHERE job_id = :j AND kind = 'page'"
            " AND state = 'complete' ORDER BY size_bytes DESC, id LIMIT 1"),
            {"j": uuid.UUID(job_id)})).scalar_one()  # fmt: skip
    path = f"/v1/evidence/{evidence_id}/content"
    await full_length(api, tenant, path, "purpose=download")
    await cut_every_way(api, tenant, path, "purpose=download", route="evidence_content")


async def test_a_render_file_is_cut_when_the_session_is_revoked(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, job_id = await sealed_job(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        render = await wait_render(
            c, (await c.post(f"/v1/jobs/{job_id}/renders", json={})).json()["id"]
        )
    path = f"/v1/renders/{render['id']}/files/0/content"
    await full_length(api, tenant, path)
    await cut_every_way(api, tenant, path, route="render_file_content")
