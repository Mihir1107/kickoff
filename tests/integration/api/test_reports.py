"""Reports API (M16 step 5, ADR 0018 §2, §9, §10, §27): who may read and request reports, manual
regeneration with who and why (every generation listed, none hidden), audited downloads anchored
before the first byte, the report package (deterministic, verified offline, the chain-derived
content recomputed from the job package), and report files kept out of the evidence endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import io
import subprocess
import sys
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from temporalio.worker import Worker

import edisc_renderers.report.model as report_model
from edisc_connector_dummy.connector import DummyConnector
from edisc_db.session import tenant_tx
from edisc_worker.activities import Activities
from edisc_worker.contracts import task_queue
from edisc_worker.reports import ReportActivities
from edisc_worker.workflows import ReportWorkflow

from ..worker.conftest import WORKFLOWS
from .conftest import Api, TenantCtx, add_principal
from .first_byte import first_byte
from .test_jobs import custody, make_world, wait_status

READERS = ("tenant_admin", "client_admin", "matter_manager", "auditor", "reviewer", "collector")
CREATORS = ("tenant_admin", "matter_manager")


@pytest.fixture
async def report_worker(api: Api) -> AsyncIterator[None]:
    """The collection worker (collect-dummy) plus a report worker of this runtime (no PDF on a test
    stack: toolchain `none`), on the queues the API uses."""
    collect = Activities(
        api.sessions, api.s3, api.settings.model_copy(update={"activity_time_box_seconds": 30}),
        {"dummy": DummyConnector(api.resources.limiter)}, api.temporal,
    )  # fmt: skip
    reporting = ReportActivities(api.sessions, api.s3, api.settings)
    async with (
        Worker(api.temporal, task_queue=task_queue("dummy"), workflows=WORKFLOWS,
               activities=collect.all()),
        Worker(api.temporal, task_queue=reporting.task_queue, workflows=[ReportWorkflow],
               activities=reporting.all()),
    ):  # fmt: skip
        yield


def _cli(*args: str | Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "edisc_custody.cli", *map(str, args)],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=300, check=False,
    )  # fmt: skip
    return proc.returncode, proc.stdout + proc.stderr


async def wait_report(c: httpx.AsyncClient, report_id: str, within: float = 90) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + within
    while True:
        r = (await c.get(f"/v1/reports/{report_id}")).json()
        if r["sealed"] or asyncio.get_running_loop().time() > deadline:
            return r
        await asyncio.sleep(0.3)


async def _sealed_job(api: Api, t: TenantCtx) -> tuple[Any, str]:
    w = await make_world(api, t)
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    job = await wait_status(api, t, job_id)
    assert job["sealed"], job
    return w, job_id


async def _reported(
    api: Api, t: TenantCtx, reason: str = "first look"
) -> tuple[Any, str, dict[str, Any]]:
    w, job_id = await _sealed_job(api, t)
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        made = await c.post(f"/v1/jobs/{job_id}/reports", json={"reason": reason})
        assert made.status_code == 201, made.text
        report = await wait_report(c, made.json()["id"])
    assert (report["status"], report["sealed"]) == ("completed", True), report
    return w, job_id, report


async def _audits(api: Api, t: TenantCtx, *kinds: str) -> list[Any]:
    return [e for e in await custody(api, t, str(t.tenant_id)) if e.event_type in kinds]


# ------------------------------------------------------------------ who may do what
async def test_every_role_reads_reports_and_only_managers_and_admins_request_them(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    w, job_id, report = await _reported(api, tenant)
    matter, client = uuid.UUID(w.matter), uuid.UUID(w.client)
    rid = report["id"]
    reads = [f"/v1/jobs/{job_id}/reports", f"/v1/reports/{rid}", f"/v1/reports/{rid}/files",
             f"/v1/reports/{rid}/files/report.json/content", f"/v1/reports/{rid}/package"]  # fmt: skip
    for role in READERS[1:]:  # tenant_admin is the default token
        scope, sid = ("client", client) if role == "client_admin" else ("matter", matter)
        _, subject = await add_principal(api, tenant, roles=[(role, scope, sid)])
        async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
            for url in reads:
                assert (await c.get(url)).status_code == 200, (role, url)
            posted = await c.post(f"/v1/jobs/{job_id}/reports", json={"reason": f"by {role}"})
            assert posted.status_code == (201 if role in CREATORS else 403), (role, posted.text)
    _, outsider = await add_principal(api, tenant, roles=[("auditor", "matter", uuid.uuid4())])
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=outsider)) as c:
        assert (await c.get(f"/v1/reports/{rid}")).status_code == 404  # cannot see it at all
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        assert (await c.get(f"/v1/reports/{uuid.uuid4()}")).status_code == 404
        assert (await c.get(f"/v1/reports/{rid}/files/nope.json/content")).status_code == 404


async def test_manual_regeneration_records_who_and_why_and_never_hides_earlier_reports(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    _, job_id, first = await _reported(api, tenant, reason="initial request")
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for bad in ({}, {"reason": ""}, {"reason": "x" * 2001}, {"reason": "ok", "paper": "a3"},
                    {"reason": "ok", "extra": 1}):  # fmt: skip
            assert (await c.post(f"/v1/jobs/{job_id}/reports", json=bad)).status_code == 422, bad
        again = await c.post(
            f"/v1/jobs/{job_id}/reports", json={"reason": "counsel asked for A4", "paper": "a4"}
        )
        assert again.status_code == 201, again.text
        second = await wait_report(c, again.json()["id"])
        listed = (await c.get(f"/v1/jobs/{job_id}/reports")).json()["items"]
    assert second["status"] == "completed" and second["paper"] == "a4"
    assert [r["id"] for r in listed] == [first["id"], second["id"]]  # both, oldest first
    assert [r["request_reason"] for r in listed] == ["initial request", "counsel asked for A4"]
    assert all(r["requested_by"].startswith("user:") for r in listed)
    assert first["snapshot_digest"] != second["snapshot_digest"]  # a new snapshot each time
    requested = await _audits(api, tenant, "audit.report_requested")
    assert [e.payload["reason"] for e in requested] == ["initial request", "counsel asked for A4"]
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        started = (
            await s.execute(
                text("SELECT payload FROM custody_events WHERE stream_id = :r AND seq = 1"),
                {"r": uuid.UUID(second["id"])},
            )
        ).scalar_one()
    assert started["reason"] == "counsel asked for A4" and started["requested_by"].startswith(
        "user:"
    )


async def test_a_job_that_is_not_sealed_gets_409_and_nothing_is_created(
    api: Api, tenant: TenantCtx
) -> None:
    w = await make_world(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
        r = await c.post(f"/v1/jobs/{job_id}/reports", json={"reason": "too early"})
        assert (r.status_code, r.json()["error"]) == (409, "job_not_sealed")
        assert (await c.get(f"/v1/jobs/{job_id}/reports")).json()["items"] == []


# ------------------------------------------------------------------ downloads
async def test_files_are_served_audited_with_their_exact_length(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    _, _, report = await _reported(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        files = (await c.get(f"/v1/reports/{report['id']}/files")).json()
        assert {f["name"] for f in files} >= {"report.json", "report.html", "units.jsonl"}
        for f in files:
            got = await c.get(f"/v1/reports/{report['id']}/files/{f['name']}/content")
            assert got.status_code == 200
            assert int(got.headers["content-length"]) == len(got.content) == f["size"]
            assert got.headers["x-evidence-sha256"] == f["sha256"]
            assert got.headers["content-type"].startswith(f["media_type"].split(";")[0])
    reads = await _audits(api, tenant, "audit.report_read")
    assert sorted(e.payload["name"] for e in reads) == sorted(f["name"] for f in files)
    assert all(e.payload["report_id"] == report["id"] and e.payload["request_id"] for e in reads)


async def test_every_content_read_is_audited_and_anchored_before_the_first_byte(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    _, _, report = await _reported(api, tenant)
    rid = report["id"]
    for path, kind in ((f"/v1/reports/{rid}/files/report.html/content", "audit.report_read"),
                       (f"/v1/reports/{rid}/package", "audit.report_package_read")):  # fmt: skip
        got = await first_byte(api, tenant, path)
        assert got.status == 200
        assert got.newest.event_type == kind, got.newest
        assert got.anchored >= got.newest.seq  # committed AND anchored before the first byte


async def test_report_files_are_never_served_by_the_evidence_endpoint(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    _, _, report = await _reported(api, tenant)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        ids = (
            (
                await s.execute(
                    text("SELECT evidence_object_id FROM report_files WHERE report_id = :r"),
                    {"r": uuid.UUID(report["id"])},
                )
            )
            .scalars()
            .all()
        )
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for evidence_id in ids:
            got = await c.get(f"/v1/evidence/{evidence_id}/content", params={"purpose": "preview"})
            assert got.status_code == 404


async def test_the_package_is_deterministic_and_recomputes_from_the_job_package(
    api: Api, tenant: TenantCtx, report_worker: None, tmp_path: Path
) -> None:
    _, job_id, report = await _reported(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        packages = [await c.get(f"/v1/reports/{report['id']}/package") for _ in range(2)]
        job_pkg = await c.get(f"/v1/jobs/{job_id}/package")
    assert packages[0].content == packages[1].content
    assert int(packages[0].headers["content-length"]) == len(packages[0].content)
    with zipfile.ZipFile(io.BytesIO(packages[0].content)) as zf:
        assert "outputs/report.json" in zf.namelist()
    (tmp_path / "report.zip").write_bytes(packages[0].content)
    (tmp_path / "job.zip").write_bytes(job_pkg.content)
    code, out = _cli(tmp_path / "report.zip")
    assert code == 0, out
    assert "not recomputed (no --job-package given)" in out
    code, out = _cli(tmp_path / "report.zip", "--job-package", tmp_path / "job.zip")
    assert code == 0, out
    assert "RECOMPUTED from the job chain: identical" in out
    reads = await _audits(api, tenant, "audit.report_package_read")
    assert [e.payload["manifest_sha256"] for e in reads] == [
        p.headers["x-manifest-sha256"] for p in packages
    ]


async def test_a_tampered_chain_or_a_tampered_report_fails_verification(
    api: Api, tenant: TenantCtx, report_worker: None, tmp_path: Path
) -> None:
    _, job_id, report = await _reported(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        rpkg = (await c.get(f"/v1/reports/{report['id']}/package")).content
        jpkg = (await c.get(f"/v1/jobs/{job_id}/package", params={"objects": "omit"})).content
    good_report, good_job = _unzip(rpkg, tmp_path / "r"), _unzip(jpkg, tmp_path / "j")
    assert _cli(good_report, "--job-package", good_job)[0] == 0

    # a tampered chain: one unit event of the job says another count, and the forger even fixes the
    # package manifest's file hash: the event's own hash no longer matches its content
    bad_job = _unzip(jpkg, tmp_path / "j2")
    _edit_jsonl(bad_job / "events.jsonl", "unit_reconciled", "collected", 999)
    code, out = _cli(good_report, "--job-package", bad_job)
    assert code == 1 and "event_hash mismatch" in out, out

    # a tampered report: a units.jsonl row states another count (its recorded hash differs)
    bad_report = _unzip(rpkg, tmp_path / "r2")
    lines = (bad_report / "outputs" / "units.jsonl").read_bytes().splitlines(keepends=True)
    lines[0] = lines[0].replace(b'"collected":', b'"collected":1', 1)
    (bad_report / "outputs" / "units.jsonl").write_bytes(b"".join(lines))
    code, out = _cli(bad_report, "--job-package", good_job)
    assert code == 1 and "outputs/units.jsonl" in out, out


async def test_a_report_built_to_other_facts_than_the_chain_fails_the_recomputation(
    api: Api, tenant: TenantCtx, report_worker: None, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """A report whose files are internally consistent and sealed (every hash matches its record) but
    state chain facts other than the chain's (faulty or compromised report code): only the
    recomputation from the job package can tell, and it does."""
    real = report_model.unit_row

    def lying(row: Any, **kw: Any) -> dict[str, Any]:
        out = real(row, **kw)
        if out["source"] == "job_chain" and out["expected"] is not None:
            out["expected"] += 1
        return out

    monkeypatch.setattr(report_model, "unit_row", lying)
    _, job_id, report = await _reported(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        rpkg = (await c.get(f"/v1/reports/{report['id']}/package")).content
        jpkg = (await c.get(f"/v1/jobs/{job_id}/package", params={"objects": "omit"})).content
    r, j = _unzip(rpkg, tmp_path / "r"), _unzip(jpkg, tmp_path / "j")
    assert _cli(r)[0] == 0  # alone, the package is consistent
    code, out = _cli(r, "--job-package", j)
    assert code == 1, out
    assert "recomputed from the job chain: unit" in out and "expected" in out, out


def _unzip(data: bytes, dest: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)
    return dest


def _edit_jsonl(path: Path, event_type: str, field: str, value: Any) -> None:
    import json

    out = []
    done = False
    for raw in path.read_bytes().splitlines():
        rec = json.loads(raw)
        if not done and rec["fields"]["event_type"] == event_type:
            rec["fields"]["payload"][field] = value
            done = True
        out.append(json.dumps(rec, separators=(",", ":")).encode())
    assert done
    data = b"\n".join(out) + b"\n"
    path.write_bytes(data)
    manifest = json.loads((path.parent / "manifest.json").read_bytes())
    manifest["files"][path.name] = {"sha256": hashlib.sha256(data).hexdigest(), "lines": len(out)}
    (path.parent / "manifest.json").write_text(json.dumps(manifest))


async def test_a_file_whose_stored_bytes_differ_aborts_the_read_with_an_alert(
    api: Api, tenant: TenantCtx, report_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import edisc_api.routes.reports as routes
    from edisc_custody.report_export import PackageIntegrityError

    _, _, report = await _reported(api, tenant)
    real_open = routes.EvidenceWriter.open

    async def altered(self: Any, **kw: Any) -> AsyncIterator[bytes]:
        first = True
        async for chunk in real_open(self, **kw):
            yield (bytes([chunk[0] ^ 1]) + chunk[1:]) if first and chunk else chunk
            first = False

    monkeypatch.setattr(routes.EvidenceWriter, "open", altered)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        with pytest.raises(PackageIntegrityError, match=r"report\.json"):
            await c.get(f"/v1/reports/{report['id']}/files/report.json/content")
    events = await _audits(api, tenant, "audit.report_read", "audit.report_read_aborted")
    assert [e.event_type for e in events] == ["audit.report_read", "audit.report_read_aborted"]
    assert events[1].payload["integrity"] is True
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        alerts = (
            (
                await s.execute(
                    text("SELECT message FROM alerts WHERE kind = 'report_read_mismatch'")
                )
            )
            .scalars()
            .all()
        )
    assert len(alerts) == 1 and report["id"] in alerts[0]


async def test_a_package_object_that_differs_from_its_record_aborts_the_stream(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    from edisc_custody.report_export import PackageIntegrityError

    from ..custody.conftest import superuser

    _, _, report = await _reported(api, tenant)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        seal_key = (
            await s.execute(
                text("SELECT job_seal_key FROM reports WHERE id = :r"),
                {"r": uuid.UUID(report["id"])},
            )
        ).scalar_one()
    conn = await superuser(api.settings)
    try:  # the registry's record of the job's seal anchor no longer matches the WORM bytes
        await conn.execute(
            "UPDATE evidence_objects SET sha256 = $1, source_sha256 = $1 WHERE storage_key = $2",
            "ef" * 32,
            seal_key,
        )
    finally:
        await conn.close()
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        with pytest.raises(PackageIntegrityError, match="objects/" + "ef" * 32):
            await c.get(f"/v1/reports/{report['id']}/package")
    events = await _audits(api, tenant, "audit.report_package_read",
                           "audit.report_package_aborted")  # fmt: skip
    assert [e.event_type for e in events] == [
        "audit.report_package_read", "audit.report_package_aborted",
    ]  # fmt: skip
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        n = (
            await s.execute(
                text("SELECT count(*) FROM alerts WHERE kind = 'report_package_mismatch'")
            )
        ).scalar_one()
    assert n == 1


async def test_the_verifier_is_strict_about_unlisted_files(
    api: Api, tenant: TenantCtx, report_worker: None, tmp_path: Path
) -> None:
    _, _, report = await _reported(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        rpkg = (await c.get(f"/v1/reports/{report['id']}/package")).content
    root = _unzip(rpkg, tmp_path / "r")
    (root / "outputs" / "extra.txt").write_bytes(b"not in the report")
    code, out = _cli(root)
    assert code == 1 and "outputs/extra.txt: not part of this package" in out, out
