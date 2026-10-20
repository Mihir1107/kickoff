"""The API side of M17 step 8 (ADR 0016 A6, A2.2): the tenant's re-authorization deadline (24 h ..
90 days, refused outside, never clamped; a tenant admin's change audited) and the audited
"finish without access" action (``job.resume``, a reason, a recent sign-in; only a job paused for
re-authorization; recorded in the job's chain before any unit closes)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from edisc_api.routes.jobs import _pipeline
from edisc_connector_dummy.spec import DatasetSpec
from edisc_core.ids import new_id
from edisc_db.session import tenant_tx
from edisc_renderers.report import model as m
from edisc_worker import access_lost

from .conftest import Api, TenantCtx, add_principal, secret

SPEC = DatasetSpec.model_validate({"seed": 5, "conversations": 2, "days": 1,
                                   "messages_per_unit": 12, "page_size": 6, "users": 8})  # fmt: skip


async def _rows(api: Api, ctx: TenantCtx, sql: str, **params: Any) -> list[Any]:
    async with tenant_tx(api.sessions, ctx.tenant_id) as s:
        return list((await s.execute(text(sql), params)).all())


async def _paused_job(api: Api, t: TenantCtx) -> tuple[uuid.UUID, uuid.UUID]:
    """A running job with two unsettled units, then the connection's credentials rejected: paused
    by the same code a worker runs (``Pipeline.pause_connection``)."""
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        until = "2099-01-01T00:00:00+00:00"
        matter = (await c.post(f"/v1/clients/{t.default_client_id}/matters",
                               json={"name": "M", "retention_until": until})).json()["id"]  # fmt: skip
        conn = (await c.post(f"/v1/clients/{t.default_client_id}/connections", json={
            "source": "dummy", "external_org_id": SPEC.workspace_id,
            "config": {"spec": SPEC.model_dump(mode="json"), "epoch": 0},
            "credentials": {"access_token": secret()}})).json()["id"]  # fmt: skip
    job = new_id()
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        await s.execute(text(
            "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status,"
            " connector_version, requested_by) VALUES (:i, :t, :m, :c, 'running', '0', 'tests')"),
            {"i": job, "t": t.tenant_id, "m": uuid.UUID(matter), "c": uuid.UUID(conn)})  # fmt: skip
        for k in ("C1/2026-01-01", "C2/2026-01-01"):
            await s.execute(text(
                "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day)"
                " VALUES (:t, :j, :k, :c, '2026-01-01')"),
                {"t": t.tenant_id, "j": job, "k": k, "c": k.split("/")[0]})  # fmt: skip
    paused = await _pipeline(api.resources).pause_connection(
        tenant_id=t.tenant_id, connection_id=uuid.UUID(conn),
        reason="source rejected credentials (credentials_expired): token_expired",
    )  # fmt: skip
    assert paused == [job]
    return job, uuid.UUID(conn)


# ------------------------------------------------------------------ the tenant's deadline
async def test_the_deadline_is_a_tenant_setting_bounded_24_hours_to_90_days(
    api: Api, tenant: TenantCtx
) -> None:
    t = tenant
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        assert (await c.get("/v1/tenant/settings")).json() == {
            "reauth_deadline_hours": 168,
            "hold_cooling_off_days": 7,
        }
        for hours in (23, 2161, 0, -1):  # refused, never clamped
            r = await c.put("/v1/tenant/settings/reauth-deadline",
                            json={"hours": hours, "reason": "policy"})  # fmt: skip
            assert r.status_code == 422, r.text
        assert (await c.get("/v1/tenant/settings")).json() == {
            "reauth_deadline_hours": 168,
            "hold_cooling_off_days": 7,
        }
        for hours in (24, 2160, 48):
            r = await c.put("/v1/tenant/settings/reauth-deadline",
                            json={"hours": hours, "reason": "policy"})  # fmt: skip
            assert r.status_code == 200 and r.json() == {
                "reauth_deadline_hours": hours,
                "hold_cooling_off_days": 7,
            }
        assert (
            await c.put("/v1/tenant/settings/reauth-deadline", json={"hours": 48})
        ).status_code == 422  # a reason is required
    changes = await _rows(api, t, "SELECT payload FROM custody_events WHERE stream_id = :t AND"
                          " event_type = 'audit.tenant_setting_changed' ORDER BY seq", t=t.tenant_id)  # fmt: skip
    assert [(p.payload["old"], p.payload["new"]) for p in changes] == [
        (168, 24),
        (24, 2160),
        (2160, 48),
    ]
    _, subject = await add_principal(api, t, roles=[("matter_manager", "tenant", None)])
    async with api.client(t.subdomain, t.token(api.settings, subject=subject)) as c:
        r = await c.put("/v1/tenant/settings/reauth-deadline", json={"hours": 72, "reason": "x"})
        assert r.status_code == 403


@pytest.mark.parametrize(("hours", "warn_before_deadline"), [(24, 12 * 3600), (168, 24 * 3600)])
async def test_a_pause_takes_its_deadline_from_the_tenant_and_warns_before_it(
    api: Api, tenant: TenantCtx, hours: int, warn_before_deadline: int
) -> None:
    """The warning comes 24 h before the deadline, or at half of it under 48 h (A2.1)."""
    t = tenant
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        r = await c.put("/v1/tenant/settings/reauth-deadline",
                        json={"hours": hours, "reason": "policy"})  # fmt: skip
        assert r.status_code == 200
    job, conn = await _paused_job(api, t)
    (p,) = await _rows(api, t, "SELECT * FROM job_pauses WHERE job_id = :j", j=job)
    assert p.reason_class == "credentials_expired"
    assert (p.deadline_at - p.paused_at).total_seconds() == hours * 3600
    assert (p.deadline_at - p.warn_at).total_seconds() == warn_before_deadline
    (a,) = await _rows(api, t, "SELECT kind, connection_id, message FROM alerts WHERE job_id = :j",
                       j=job)  # fmt: skip
    assert (a.kind, a.connection_id) == ("job_paused", conn) and str(job) in a.message
    (e,) = await _rows(api, t, "SELECT payload FROM custody_events WHERE stream_id = :j"
                       " AND event_type = 'job_paused'", j=job)  # fmt: skip
    assert e.payload["reason_class"] == "credentials_expired" and e.payload["deadline_at"]


# ------------------------------------------------------------------ finish without access
async def test_finish_without_access_is_audited_recorded_and_closes_the_units(
    api: Api, tenant: TenantCtx, sweeper_sessions: Any
) -> None:
    t = tenant
    job, _ = await _paused_job(api, t)
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        path = f"/v1/jobs/{job}/finish-without-access"
        assert (await c.post(path, json={})).status_code == 422  # a reason is required
        r = await c.post(path, json={"reason": "the customer left the workspace"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paused_awaiting_reauth"  # decided; units close next
        r = await c.post(path, json={"reason": "again"})
        assert (r.status_code, r.json()["error"]) == (409, "conflict")  # decided once
    (audit,) = await _rows(api, t, "SELECT actor, payload FROM custody_events WHERE stream_id = :t"
                           " AND event_type = 'audit.job_finished_without_access'", t=t.tenant_id)  # fmt: skip
    assert audit.payload["job_id"] == str(job) and audit.payload["reason"]
    assert audit.payload["authenticated_at"] and audit.actor.startswith("user:")
    (ended,) = await _rows(api, t, "SELECT actor, payload FROM custody_events WHERE stream_id = :j"
                           " AND event_type = 'job_pause_ended'", j=job)  # fmt: skip
    assert ended.payload["ended_how"] == "finished_without_access"
    assert ended.payload["reason_class"] == "credentials_expired" and ended.actor == audit.actor
    # decided: a re-authorization now no longer resumes the job (the decision stands)
    (conn,) = await _rows(api, t, "SELECT connection_id FROM collection_jobs WHERE id = :j", j=job)
    assert (
        await _pipeline(api.resources).resume_connection(
            tenant_id=t.tenant_id, connection_id=conn.connection_id, actor="user:late"
        )
        == []
    )
    await access_lost.check_reauth_deadlines(sweeper_sessions, _pipeline(api.resources), None,
                                             tenant_id=t.tenant_id)  # fmt: skip
    units = await _rows(api, t, "SELECT status, recon_status, access_lost_reason FROM work_units"
                        " WHERE job_id = :j", j=job)  # fmt: skip
    assert {tuple(u) for u in units} == {("done", "access_lost", "credentials_expired")}
    (j,) = await _rows(api, t, "SELECT stop_reason FROM collection_jobs WHERE id = :j", j=job)
    assert j.stop_reason == "access_lost"
    # what the report states of this pause, folded from the job's chain as the report does
    fold = m.ChainFold()
    for e in await _rows(api, t, "SELECT seq, event_type, actor, created_at, payload FROM"
                         " custody_events WHERE stream_id = :j ORDER BY seq", j=job):  # fmt: skip
        fold.add(m.ChainEvent(e.seq, e.event_type, e.actor, e.created_at, e.payload))
    (stated,) = m.pauses_section(fold.pauses)["pauses"]
    assert (stated["ended_how"], stated["ended_by"], stated["end_reason"]) == (
        "finished_without_access", audit.actor, "the customer left the workspace")  # fmt: skip
    assert stated["reason_class"] == "credentials_expired" and stated["ended_at"]


async def test_finish_without_access_needs_a_paused_job_and_the_permission(
    api: Api, tenant: TenantCtx
) -> None:
    t = tenant
    job, _ = await _paused_job(api, t)
    _, reviewer = await add_principal(api, t, roles=[("reviewer", "tenant", None)])
    async with api.client(t.subdomain, t.token(api.settings, subject=reviewer)) as c:
        r = await c.post(f"/v1/jobs/{job}/finish-without-access", json={"reason": "x"})
        assert r.status_code == 403
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        r = await c.post(f"/v1/jobs/{uuid.uuid4()}/finish-without-access", json={"reason": "x"})
        assert r.status_code == 404
    async with tenant_tx(api.sessions, t.tenant_id) as s:  # a job that is not paused
        await s.execute(text("UPDATE collection_jobs SET status = 'running' WHERE id = :j"),
                        {"j": job})  # fmt: skip
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        r = await c.post(f"/v1/jobs/{job}/finish-without-access", json={"reason": "x"})
        assert r.status_code == 409
    assert await _rows(api, t, "SELECT 1 FROM custody_events WHERE stream_id = :j"
                       " AND event_type = 'job_pause_ended'", j=job) == []  # fmt: skip
