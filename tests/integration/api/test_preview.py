"""The HTML preview (M16 step 6, ADR 0018 §1.2, §2, §4.7, §10, §15): ``GET /v1/jobs/{id}/preview``
under ``evidence.read``, every in-scope message of the day once (the dataset is the oracle), pages,
time-zone days, the audit committed and anchored before the first byte with the served bytes' SHA-256,
the CSP and the other headers, refusals audited, attachment links resolving to the pinned evidence,
and a day of a long conversation read with a bounded number of queries."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, text

import edisc_renderers.preview as preview_module
from edisc_connector_dummy.dataset import Dataset
from edisc_db.session import tenant_tx
from edisc_renderers.preview import CSP_HEADER
from edisc_worker.preview import build_preview

from ..worker.conftest import spec
from .conftest import Api, TenantCtx, add_principal, secret
from .first_byte import first_byte
from .test_jobs import custody, make_world, wait_status
from .test_reports import report_worker

__all__ = ["report_worker"]  # the fixture, re-exported for pytest

PRIMARY = re.compile(r'<li class="ev" id="m-([0-9-]+)">')
CONTEXT = re.compile(r'<li class="ev ctx" id="m-([0-9-]+)">')
EVIDENCE = re.compile(r'href="(/v1/evidence/[0-9a-f-]{36}/content\?purpose=preview)"')


def _anchor(ts: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in ts)


async def _job(api: Api, t: TenantCtx, **sp: Any) -> tuple[Any, str]:
    w = await make_world(api, t, spec(p_file=0.4, **sp))
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    job = await wait_status(api, t, job_id)
    assert job["sealed"], job
    return w, job_id


async def _audits(api: Api, t: TenantCtx, *kinds: str) -> list[Any]:
    return [e for e in await custody(api, t, str(t.tenant_id)) if e.event_type in kinds]


def _url(job_id: str) -> str:
    return f"/v1/jobs/{job_id}/preview"


async def test_every_page_holds_the_days_messages_once_and_only_readers_of_evidence_see_it(
    api: Api, tenant: TenantCtx, report_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, job_id = await _job(api, tenant)
    ds = Dataset(w.sp)
    monkeypatch.setattr(preview_module, "PREVIEW_PAGE_EVENTS", 5)  # several pages per day
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for conv in ds.conversations():
            every = {m.ts for x in range(ds.n_days(0)) for m in ds.unit_messages(conv.id, x, 0)}
            for d in range(ds.n_days(0)):
                want = {_anchor(m.ts) for m in ds.unit_messages(conv.id, d, 0)}
                seen: list[str] = []
                page, pages = 1, 1
                while page <= pages:
                    r = await c.get(_url(job_id), params={"conversation": conv.id,
                                    "day": ds.day(d).isoformat(), "page": page})  # fmt: skip
                    assert r.status_code == 200, r.text
                    body = r.text
                    pages = int(re.search(r"<th>page</th><td>(\d+) of (\d+):", body).group(2))  # type: ignore[union-attr]
                    found = PRIMARY.findall(body)
                    shown = set(found) | set(CONTEXT.findall(body))
                    assert len(shown) <= 5
                    # every reply's thread root is on its page: a primary, or marked context
                    for m in ds.unit_messages(conv.id, d, 0):
                        reply = m.thread_ts and m.thread_ts != m.ts and m.thread_ts in every
                        if _anchor(m.ts) in found and reply:
                            assert _anchor(str(m.thread_ts)) in shown, (conv.id, m.ts)
                    seen += found
                    page += 1
                assert sorted(seen) == sorted(want), (conv.id, d)  # every message once
                assert (await c.get(_url(job_id), params={"conversation": conv.id,
                        "day": ds.day(d).isoformat(), "page": pages + 1})).status_code == 404  # fmt: skip
    matter, client = uuid.UUID(w.matter), uuid.UUID(w.client)
    params = {"conversation": ds.conversations()[0].id, "day": ds.day(0).isoformat()}
    for role, scope, sid, status in [
        ("reviewer", "matter", matter, 200), ("matter_manager", "matter", matter, 200),
        ("client_admin", "client", client, 200), ("auditor", "matter", matter, 403),
        ("collector", "matter", matter, 403),
    ]:  # fmt: skip
        _, subject = await add_principal(api, tenant, roles=[(role, scope, sid)])
        async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
            assert (await c.get(_url(job_id), params=params)).status_code == status, role
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        assert (await c.get(_url(str(uuid.uuid4())), params=params)).status_code == 404


async def test_the_read_is_audited_and_anchored_before_the_first_byte_with_its_hash(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    w, job_id = await _job(api, tenant)
    ds = Dataset(w.sp)
    conv, day = ds.conversations()[0].id, ds.day(0).isoformat()
    got = await first_byte(api, tenant, _url(job_id), f"conversation={conv}&day={day}")
    assert got.status == 200
    assert got.newest.event_type == "audit.preview_read", got.newest
    assert got.anchored >= got.newest.seq
    payload = got.newest.payload
    assert (payload["conversation_id"], payload["day"], payload["zone"], payload["page"]) == (
        conv, day, "UTC", 1,
    )  # fmt: skip
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        r = await c.get(_url(job_id), params={"conversation": conv, "day": day})
    (read,) = (await _audits(api, tenant, "audit.preview_read"))[-1:]
    assert (
        read.payload["sha256"]
        == hashlib.sha256(r.content).hexdigest()
        == r.headers["x-preview-sha256"]
    )
    assert read.payload["size"] == len(r.content) and read.payload["slice_source_hash"]
    assert read.payload["identity"]["preview_renderer"] == preview_module.PREVIEW_RENDERER_VERSION
    # headers (D10)
    assert r.headers["content-security-policy"] == CSP_HEADER
    assert "default-src 'none'" in CSP_HEADER and "sandbox" in CSP_HEADER
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    # deterministic for this runtime
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        again = await c.get(_url(job_id), params={"conversation": conv, "day": day})
    assert again.content == r.content


async def test_refusals_are_audited_and_never_an_empty_page(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    w, job_id = await _job(api, tenant)
    ds = Dataset(w.sp)
    conv = ds.conversations()[0].id
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        outside = (ds.day(0) - timedelta(days=30)).isoformat()
        r = await c.get(_url(job_id), params={"conversation": conv, "day": outside})
        assert (r.status_code, r.json()["error"]) == (404, "not_in_scope")
        # canaries (`secret`): the client's scan fails the test if an answer quotes them (D11)
        r = await c.get(
            _url(job_id), params={"conversation": secret("CNOPE"), "day": ds.day(0).isoformat()}
        )
        assert (r.status_code, r.json()["error"]) == (404, "not_in_scope")
        r = await c.get(_url(job_id), params={"conversation": conv, "day": ds.day(0).isoformat(),
                                               "zone": secret("Mars/Olympus")})  # fmt: skip
        assert (r.status_code, r.json()["error"]) == (422, "unknown_time_zone")
        r = await c.get(_url(job_id), params={"conversation": conv, "day": ds.day(0).isoformat(),
                                               "page": 99})  # fmt: skip
        assert (r.status_code, r.json()["error"]) == (404, "page_not_found")
        unsealed = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
        r = await c.get(_url(unsealed), params={"conversation": conv, "day": ds.day(0).isoformat()})
        assert r.status_code in (409, 200)  # 409 while it runs (it may finish first)
    codes = [e.payload["code"] for e in await _audits(api, tenant, "audit.preview_refused")]
    assert codes[:4] == ["not_in_scope", "not_in_scope", "unknown_time_zone", "page_not_found"]


async def test_attachment_links_resolve_to_the_pinned_evidence(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    w, job_id = await _job(api, tenant)
    ds = Dataset(w.sp)
    links: list[str] = []
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for conv in ds.conversations():
            for d in range(ds.n_days(0)):
                r = await c.get(_url(job_id), params={"conversation": conv.id,
                                                      "day": ds.day(d).isoformat()})  # fmt: skip
                links += EVIDENCE.findall(r.text)
        assert links, "the dataset has collected files"
        for link in links[:5]:
            got = await c.get(link)
            assert got.status_code == 200, link
            assert hashlib.sha256(got.content).hexdigest() == got.headers["x-evidence-sha256"]
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        kinds = (await s.execute(text(
            "SELECT DISTINCT kind FROM evidence_objects WHERE id = ANY(:i)"
        ), {"i": [uuid.UUID(x.split("/")[3]) for x in links]})).scalars().all()  # fmt: skip
    assert set(kinds) == {"file"}


async def test_a_day_in_a_time_zone_holds_the_messages_of_its_local_day(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    w, job_id = await _job(api, tenant)
    ds = Dataset(w.sp)
    from edisc_renderers.rsmf import load_zone, slice_bounds

    zone = "Asia/Kathmandu"  # +05:45: the local day straddles two UTC days
    day = ds.day(1)
    start, end = slice_bounds(day, load_zone(zone))
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for conv in ds.conversations():
            want = {
                _anchor(m.ts)
                for d in range(ds.n_days(0))
                for m in ds.unit_messages(conv.id, d, 0)
                if start <= m.sent_at < end
            }
            r = await c.get(_url(job_id), params={"conversation": conv.id,
                                                  "day": day.isoformat(), "zone": zone})  # fmt: skip
            assert r.status_code == 200, r.text
            assert sorted(PRIMARY.findall(r.text)) == sorted(want), conv.id
            assert zone in r.text


@contextmanager
def _counting(api: Api) -> Iterator[list[str]]:
    engine = api.sessions.kw["bind"].sync_engine
    seen: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_one_day_of_a_long_conversation_is_read_with_a_bounded_number_of_queries(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    """The same day previewed in a 2-day and a 12-day job: a bounded number of statements, none of
    them the whole conversation's links (the render's `_index`), so a page's cost does not grow with
    the conversation."""
    counts = []
    for days in (2, 12):
        w, job_id = await _job(api, tenant, days=days, conversations=1, messages_per_unit=12)
        ds = Dataset(w.sp)
        with _counting(api) as seen:
            await build_preview(
                api.sessions, api.s3, api.settings, tenant_id=tenant.tenant_id,
                job_id=uuid.UUID(job_id), conversation_id=ds.conversations()[0].id,
                day=ds.day(1), time_zone="UTC", page=1,
            )  # fmt: skip
        queries = [
            q for q in seen if not q.lstrip().upper().startswith(("SET", "BEGIN", "SELECT SET"))
        ]
        assert not [q for q in queries if "unit_key = ANY" in q and "job_items" in q], queries
        counts.append(len(queries))
    # each tenant_tx also sets the tenant and timeouts; 2 and 12 days measured 37 statements each
    assert max(counts) <= 45 and abs(counts[0] - counts[1]) <= 3, counts
