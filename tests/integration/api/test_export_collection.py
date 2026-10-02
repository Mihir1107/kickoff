"""Collecting from a validated Slack export (ADR 0014 M14.5) through the real API, Temporal workers,
Postgres and MinIO. Only the external file host behind export links is faked.

- cross-source identity: one dummy dataset collected via the live Web API dialect AND as an export
  gives zero duplicate versions; the export job ends ``completed_against_archive`` with the caveat;
- thread context across day files under each thread-parent policy, from the validation index;
- file links: downloaded through the limiter; expired, revoked, unreachable or foreign links become
  recorded file gaps with their reason, never a stall;
- R4: a message whose ts is not on its file's day is placed by its ts and reported as an anomaly;
- Grid: items are namespaced by their conversation's own team, not by users.json.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportOptions, write_export
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connector_dummy.zipwriter import ZipWriter
from edisc_core.schemas import ARCHIVE_CAVEAT
from edisc_db.session import tenant_tx

from .conftest import Api, FileHost, TenantCtx, collection_workers
from .test_exports import settled, upload
from .test_jobs import make_world

SPEC = DatasetSpec(
    seed=31, dialect="slack_history", conversations=4, days=3, messages_per_unit=12, p_file=0.3
)


async def _job(c: httpx.AsyncClient, matter: str, body: dict[str, Any]) -> dict[str, Any]:
    r = await c.post(f"/v1/matters/{matter}/jobs", json=body)
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    async with asyncio.timeout(120):
        while True:
            job: dict[str, Any] = (await c.get(f"/v1/jobs/{job_id}")).json()
            if job["sealed"]:
                return job
            await asyncio.sleep(0.3)


def _scope(start: datetime, days: int, policy: str = "include_parent_and_thread") -> dict[str, Any]:
    return {
        "type": "channel",
        "external_id": "*",
        "date_from": start.isoformat(),
        "date_to": (start + timedelta(days=days)).isoformat(),
        "thread_parent_policy": policy,
    }


async def _linked(api: Api, t: TenantCtx, job_id: str, *, in_scope: bool | None = None) -> set[str]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT i.source_item_id FROM job_items ji JOIN items i ON i.id = ji.item_id"
                    " WHERE ji.job_id = :j AND i.item_type = 'message'"
                    " AND (CAST(:s AS boolean) IS NULL OR ji.in_scope = :s)"
                ),
                {"j": uuid.UUID(job_id), "s": in_scope},
            )
        ).scalars()
        return set(rows)


async def _export_connection(c: httpx.AsyncClient, client: str, data: bytes) -> dict[str, Any]:
    export_id, done = await upload(c, uuid.UUID(client), data)
    assert done.status_code == 202, done.text
    out = await settled(c, export_id)
    assert out["status"] == "ready", out
    return out


# ------------------------------------------------------------------ cross-source identity
async def test_live_api_and_export_collections_of_one_dataset_share_identity(
    api: Api, tenant: TenantCtx
) -> None:
    ds = Dataset(SPEC)
    host = FileHost(ds)
    buf = io.BytesIO()
    write_export(ds, buf, ExportOptions())
    async with collection_workers(api, host) as exp:
        w = await make_world(exp, tenant, SPEC)
        start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, buf.getvalue())
            assert export["workspace_id"] == SPEC.workspace_id  # from users.json
            live = await _job(
                c, w.matter, {"connection_id": w.connection, "scopes": [_scope(start, 3)]}
            )
            async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
                items_after_live = (
                    await s.execute(text("SELECT count(*) FROM items"))
                ).scalar_one()
            archive = await _job(
                c,
                w.matter,
                {"connection_id": export["connection_id"], "scopes": [_scope(start, 3)]},
            )
            recon = (await c.get(f"/v1/jobs/{archive['id']}/reconciliation")).json()
            units = (await c.get(f"/v1/jobs/{archive['id']}/units?limit=200")).json()["items"]
            entry_ev = await _first_entry_evidence(exp, tenant, archive["id"])
            content = await c.get(f"/v1/evidence/{entry_ev}/content", params={"purpose": "preview"})

    assert (live["status"], live["clean"], live["clean_basis"], live["caveat"]) == (
        "completed",
        True,
        "source",
        None,
    )
    assert (archive["status"], archive["clean"], archive["clean_basis"]) == (
        "completed_against_archive",
        False,
        "archive",
    )
    assert archive["caveat"] == recon["caveat"] == ARCHIVE_CAVEAT  # verbatim in every response
    assert set(recon["by_recon_status"]) == {"matched_against_archive"}
    day_units = [u for u in units if u["kind"] == "conversation_day"]
    assert day_units and all(u["recon_status"] == "matched_against_archive" for u in day_units)
    assert all(u["caveat"] == ARCHIVE_CAVEAT for u in day_units)
    assert recon["not_matched"] == []

    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        duplicates = (
            (
                await s.execute(
                    text(
                        "SELECT source_item_id FROM items WHERE item_type IN ('message', 'file')"
                        " GROUP BY source_item_id HAVING count(*) > 1"
                    )
                )
            )
            .scalars()
            .all()
        )
        items_after_both = (await s.execute(text("SELECT count(*) FROM items"))).scalar_one()
        kinds = dict(
            (
                await s.execute(
                    text(
                        "SELECT kind, count(*) FROM evidence_objects WHERE job_id = :j GROUP BY kind"
                    ),
                    {"j": uuid.UUID(archive["id"])},
                )
            ).all()
        )
    assert duplicates == []  # the same message from both sources is one item, one version
    # ... and nothing at all was new: no message, file, reaction or identity version (identity streams
    # legitimately have several versions, e.g. a rename mid-dataset, but the export added none)
    assert items_after_both == items_after_live
    assert await _linked(exp, tenant, live["id"], in_scope=True) == await _linked(
        exp, tenant, archive["id"], in_scope=True
    )
    assert kinds.get("archive_entry", 0) > 0 and "page" not in kinds  # referenced, never copied
    assert content.status_code == 200
    assert hashlib.sha256(content.content).hexdigest() == content.headers["x-evidence-sha256"]
    assert isinstance(json.loads(content.content), list)  # the entry as exported


async def _first_entry_evidence(api: Api, t: TenantCtx, job_id: str) -> str:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return str(
            (
                await s.execute(
                    text(
                        "SELECT id FROM evidence_objects WHERE job_id = :j AND kind = 'archive_entry'"
                        " AND entry_path LIKE '%/%' ORDER BY id LIMIT 1"
                    ),
                    {"j": uuid.UUID(job_id)},
                )
            ).scalar_one()
        )


# ------------------------------------------------------------------ handcrafted exports
def _ts(day: int, hour: int) -> str:
    return f"{int(datetime(2026, 1, day, hour, tzinfo=UTC).timestamp())}.000100"


def _msg(ts: str, text_: str, thread_ts: str | None = None, **extra: Any) -> dict[str, Any]:
    m: dict[str, Any] = {"type": "message", "user": "U1", "text": text_, "ts": ts}
    if thread_ts:
        m["thread_ts"] = thread_ts
    return {**m, **extra}


def _zip(files: dict[str, Any], channels: list[dict[str, Any]] | None = None) -> bytes:
    buf = io.BytesIO()
    zw = ZipWriter(buf)
    zw.add("users.json", json.dumps([{"id": "U1", "team_id": "T1", "name": "u"}]).encode())
    zw.add("channels.json", json.dumps(channels or [{"id": "C1", "name": "general"}]).encode())
    for name, messages in files.items():
        zw.add(name, json.dumps(messages).encode())
    zw.close()
    return buf.getvalue()


P, R1, Q = _ts(1, 10), _ts(5, 9), _ts(5, 11)
R2, S = _ts(9, 8), _ts(9, 9)
THREADS = {
    "general/2026-01-01.json": [_msg(P, "parent", P), _msg(_ts(1, 12), "unrelated")],
    "general/2026-01-05.json": [_msg(R1, "reply to an old parent", P), _msg(Q, "new parent", Q)],
    "general/2026-01-09.json": [_msg(R2, "late reply", P), _msg(S, "late reply to Q", Q)],
}


async def test_thread_context_across_day_files_per_policy(api: Api, tenant: TenantCtx) -> None:
    """Scope = 5 Jan only. P (1 Jan) is the out-of-range parent of R1; Q (in range) has a reply S on
    9 Jan. Whole files of 1 and 9 Jan are never collected: only the thread members the policy asks for,
    found through the validation index."""
    start = datetime(2026, 1, 5, tzinfo=UTC)
    async with collection_workers(api, FileHost()) as exp:
        w = await make_world(exp, tenant)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, _zip(THREADS))
            assert export["findings"]["threaded_messages"] == 5
            jobs = {
                policy: await _job(
                    c,
                    w.matter,
                    {
                        "connection_id": export["connection_id"],
                        "scopes": [_scope(start, 1, policy)],
                    },
                )
                for policy in ("include_parent_and_thread", "include_parent_only", "replies_only")
            }
    ids = {
        p: {i.rsplit("/", 1)[1] for i in await _linked(exp, tenant, j["id"])}
        for p, j in jobs.items()
    }
    in_scope = {R1, Q}
    assert ids["replies_only"] == in_scope
    assert ids["include_parent_only"] == in_scope | {P}
    assert ids["include_parent_and_thread"] == in_scope | {P, R2, S}
    for job in jobs.values():
        assert job["status"] == "completed_against_archive", job
        assert await _linked(exp, tenant, job["id"], in_scope=True) == {f"T1/C1/{R1}", f"T1/C1/{Q}"}
    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        parent = (
            await s.execute(
                text(
                    "SELECT e.entry_path, i.json_path FROM items i JOIN evidence_objects e"
                    " ON e.id = i.evidence_object_id WHERE i.source_item_id = :m AND i.item_type = 'message'"
                ),
                {"m": f"T1/C1/{P}"},
            )
        ).one()
    assert (parent.entry_path, parent.json_path) == ("general/2026-01-01.json", "$[0]")


async def test_file_links_become_recorded_gaps_never_stalls(api: Api, tenant: TenantCtx) -> None:
    def file(fid: str, host: str = "https://files.dummy.test") -> dict[str, Any]:
        return {
            "id": fid,
            "name": f"{fid}.txt",
            "mimetype": "text/plain",
            "size": 3,
            "url_private_download": f"{host}/{fid}/download/{fid}.txt?t=xoxe-export-token-{fid}",
        }

    day = [
        _msg(_ts(5, 9), "ok", files=[file("FOK")]),
        _msg(_ts(5, 10), "expired", files=[file("FEXP")]),
        _msg(_ts(5, 11), "revoked", files=[file("FREV")]),
        _msg(_ts(5, 12), "host down", files=[file("FDOWN")]),
        _msg(_ts(5, 13), "foreign host", files=[file("FEVIL", "http://169.254.169.254")]),
        _msg(_ts(5, 15), "redirected", files=[file("FHOP")]),
        _msg(_ts(5, 16), "redirected inside", files=[file("FINT")]),
        _msg(
            _ts(5, 14), "no link", files=[{"id": "FHIDDEN", "name": "x", "mimetype": "text/plain"}]
        ),
    ]
    host = FileHost()
    host.outcomes = {"FEXP": 410, "FREV": 403, "FDOWN": httpx.ConnectError("refused")}

    class Served(FileHost):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/FOK/"):
                self.requests.append("FOK")
                return httpx.Response(200, content=b"abc")
            if request.url.path.startswith("/FHOP/"):  # an allowed chain: two hops, then the file
                self.requests.append(request.url.path)
                hop = int(request.url.params.get("hop", "0"))
                if hop < 2:
                    return httpx.Response(302, headers={"location": f"/FHOP/x?hop={hop + 1}"})
                return httpx.Response(200, content=b"hop")
            if request.url.path.startswith("/FINT/"):
                self.requests.append("FINT")
                return httpx.Response(302, headers={"location": "https://127.0.0.1/FINT"})
            return host.handler(request)

    served = Served()
    async with collection_workers(api, served) as exp:
        w = await make_world(exp, tenant)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, _zip({"general/2026-01-05.json": day}))
            job = await _job(
                c,
                w.matter,
                {
                    "connection_id": export["connection_id"],
                    "scopes": [_scope(datetime(2026, 1, 5, tzinfo=UTC), 1)],
                },
            )
            units = (await c.get(f"/v1/jobs/{job['id']}/units")).json()["items"]
    assert job["status"] == "completed_with_gaps" and job["caveat"] is None
    unit = next(u for u in units if u["kind"] == "conversation_day")
    assert (unit["recon_status"], unit["file_gaps"]) == ("gap", 6)
    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        reasons = dict(
            (
                await s.execute(
                    text(
                        "SELECT d.derived->>'file_id', d.derived->>'reason' FROM items i"
                        " JOIN item_derivations d ON d.item_id = i.id WHERE i.event_kind = 'file_unavailable'"
                    )
                )
            ).all()
        )
        stored = (
            await s.execute(text("SELECT count(*) FROM items WHERE item_type = 'file'"))
        ).scalar_one()
    assert reasons == {
        "FEXP": "expired_url",
        "FREV": "permission",
        "FDOWN": "unreachable",
        "FEVIL": "external_or_hidden",  # never requested: not an allowed https host
        "FINT": "expired_url",  # redirected off the allowlist: the internal address never requested
        "FHIDDEN": "external_or_hidden",
    }
    assert stored == 2
    assert "FEVIL" not in host.requests and "FHIDDEN" not in host.requests
    assert served.requests.count("FOK") == 1
    assert served.requests.count("/FHOP/x") == 2  # FHOP stored, after following both redirects


async def test_day_anomalies_are_placed_by_ts_and_reported(api: Api, tenant: TenantCtx) -> None:
    stray = _ts(6, 2)  # in the 5 Jan file, but sent on 6 Jan (e.g. a non-UTC export day)
    day = [_msg(_ts(5, 9), "on the day"), _msg(stray, "after midnight UTC")]
    async with collection_workers(api, FileHost()) as exp:
        w = await make_world(exp, tenant)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, _zip({"general/2026-01-05.json": day}))
            job = await _job(
                c,
                w.matter,
                {
                    "connection_id": export["connection_id"],
                    "scopes": [_scope(datetime(2026, 1, 5, tzinfo=UTC), 1)],
                },
            )
            units = (await c.get(f"/v1/jobs/{job['id']}/units")).json()["items"]
            # a scope of 6 Jan alone still reads the 5 Jan file (file date +/- 1 day, R4) and finds it
            next_day = await _job(
                c,
                w.matter,
                {
                    "connection_id": export["connection_id"],
                    "scopes": [_scope(datetime(2026, 1, 6, tzinfo=UTC), 1)],
                },
            )
    assert await _linked(exp, tenant, next_day["id"], in_scope=True) == {f"T1/C1/{stray}"}
    assert export["findings"]["ts_outside_hint_day"] == {
        "count": 1,
        "sample": [f"general/2026-01-05.json: {stray}"],
    }
    unit = next(u for u in units if u["kind"] == "conversation_day")
    assert (unit["recon_status"], unit["day_anomalies"]) == ("matched_against_archive", 1)
    assert await _linked(exp, tenant, job["id"], in_scope=True) == {f"T1/C1/{_ts(5, 9)}"}
    assert await _linked(exp, tenant, job["id"], in_scope=False) == {f"T1/C1/{stray}"}
    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        payload = (
            await s.execute(
                text(
                    "SELECT payload FROM custody_events WHERE stream_id = :j"
                    " AND event_type = 'unit_reconciled' AND payload->>'unit_key' LIKE 'C1/%'"
                ),
                {"j": uuid.UUID(job["id"])},
            )
        ).scalar_one()
    assert (payload["basis"], payload["day_anomalies"]) == ("archive", 1)


async def test_grid_items_are_namespaced_by_their_conversation_team(
    api: Api, tenant: TenantCtx
) -> None:
    """One org export spanning workspaces: the team in each conversation's record namespaces its items
    (users.json's majority team T1 only where a record names none). Shape *(confirm on real export)*."""
    channels = [
        {"id": "C1", "name": "general"},
        {"id": "C2", "name": "eng", "context_team_id": "T2"},
        {"id": "C3", "name": "ops", "team_id": "T3"},
    ]
    files = {
        f"{name}/2026-01-05.json": [_msg(_ts(5, 9), name, team="T9")]  # the SENDER's team: ignored
        for name in ("general", "eng", "ops")
    }
    async with collection_workers(api, FileHost()) as exp:
        w = await make_world(exp, tenant)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, _zip(files, channels))
            job = await _job(
                c,
                w.matter,
                {
                    "connection_id": export["connection_id"],
                    "scopes": [_scope(datetime(2026, 1, 5, tzinfo=UTC), 1)],
                },
            )
    assert export["workspace_id"] == "T1"
    assert export["findings"]["conversation_teams"] == {"distinct": 2, "without_team": 1}
    assert await _linked(exp, tenant, job["id"], in_scope=True) == {
        f"T1/C1/{_ts(5, 9)}",
        f"T2/C2/{_ts(5, 9)}",
        f"T3/C3/{_ts(5, 9)}",
    }
