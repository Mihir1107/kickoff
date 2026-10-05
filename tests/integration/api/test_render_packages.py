"""Render package download (M15 step 5 part C, ADR 0015 §19): ``GET /v1/renders/{id}/package``.

Who may download (``export.read``), sealed renders only, the audit committed AND anchored before the
first byte, a package that ``edisc-verify`` accepts as downloaded (zip, both modes), byte-identical
downloads, and an aborted stream (audited, with an alert) when an object differs from its record."""

from __future__ import annotations

import asyncio
import hashlib
import io
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from edisc_api.app import create_app
from edisc_custody.render_export import PackageIntegrityError
from edisc_db.session import tenant_tx
from edisc_renderers.rsmf import RenderOptions
from edisc_worker.renders import create_render

from ..custody.conftest import superuser
from .conftest import Api, TenantCtx, add_principal
from .test_jobs import custody
from .test_renders import render_worker, sealed_job, wait_render

__all__ = ["render_worker"]  # the fixture, re-exported for pytest


def _cli(*args: str | Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "edisc_custody.cli", *map(str, args)],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=120, check=False,
    )  # fmt: skip
    return proc.returncode, proc.stdout + proc.stderr


async def _rendered(api: Api, t: TenantCtx) -> tuple[Any, dict[str, Any]]:
    w, job_id = await sealed_job(api, t)
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        render = await wait_render(
            c, (await c.post(f"/v1/jobs/{job_id}/renders", json={})).json()["id"]
        )
    assert (render["status"], render["sealed"]) == ("completed", True), render
    return w, render


async def _package_events(api: Api, t: TenantCtx) -> list[Any]:
    return [
        e
        for e in await custody(api, t, str(t.tenant_id))
        if e.event_type in ("audit.render_package_read", "audit.render_package_aborted")
    ]


async def _anchored_seq(api: Api, t: TenantCtx) -> tuple[int, int]:
    """(the audit stream's head seq, its last anchored seq)."""
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT last_seq, last_anchored_seq FROM custody_chain_heads WHERE stream_id = :t"
                ),
                {"t": t.tenant_id},
            )
        ).one()
    return int(row.last_seq), int(row.last_anchored_seq)


# ------------------------------------------------------------------ who may download
async def test_only_matter_managers_and_tenant_admins_download_packages(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    w, render = await _rendered(api, tenant)
    matter, client = uuid.UUID(w.matter), uuid.UUID(w.client)
    url = f"/v1/renders/{render['id']}/package"
    for role, scope, sid in [
        ("reviewer", "matter", matter),
        ("auditor", "matter", matter),
        ("collector", "matter", matter),
        ("client_admin", "client", client),
    ]:
        _, subject = await add_principal(api, tenant, roles=[(role, scope, sid)])
        async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
            assert (await c.get(url)).status_code == 403, role
    _, manager = await add_principal(api, tenant, roles=[("matter_manager", "matter", matter)])
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=manager)) as c:
        got = await c.get(url)
        assert got.status_code == 200, got.text
        assert got.headers["content-type"] == "application/zip"
        assert (await c.get(url, params={"outputs": "all"})).status_code == 422
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:  # tenant admin
        assert (await c.get(url, params={"outputs": "embed"})).status_code == 200
        assert (await c.get(f"/v1/renders/{uuid.uuid4()}/package")).status_code == 404
    reads = await _package_events(api, tenant)
    assert [e.event_type for e in reads] == [
        "audit.render_package_read"
    ] * 2  # refusals are not reads
    assert [e.payload["mode"] for e in reads] == ["reference", "embed"]
    assert all(e.actor.startswith("user:") for e in reads)


async def test_a_render_that_is_not_sealed_gets_409(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    w, job_id = await sealed_job(api, tenant)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:  # recorded, never picked up
        made = await create_render(
            s, tenant_id=tenant.tenant_id, job_id=uuid.UUID(job_id), matter_id=uuid.UUID(w.matter),
            options=RenderOptions(include_context=True, time_zone="Asia/Kolkata"),
            requested_by="tests",
        )  # fmt: skip
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        r = await c.get(f"/v1/renders/{made.render_id}/package")
        assert (r.status_code, r.json()["error"]) == (409, "render_not_sealed")
    assert await _package_events(api, tenant) == []


# ------------------------------------------------------------------ the package
async def test_downloaded_packages_verify_and_are_byte_identical(
    api: Api, tenant: TenantCtx, render_worker: None, tmp_path: Path
) -> None:
    _, render = await _rendered(api, tenant)
    url = f"/v1/renders/{render['id']}/package"
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        embed = [await c.get(url, params={"outputs": "embed"}) for _ in range(2)]
        reference = [await c.get(url) for _ in range(2)]
        files = (await c.get(f"/v1/renders/{render['id']}/files", params={"limit": 200})).json()
    assert embed[0].content == embed[1].content and reference[0].content == reference[1].content
    assert embed[0].content != reference[0].content
    for resp, mode in ((embed[0], "embed"), (reference[0], "reference")):
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            manifest = zf.read("manifest.json")
            outputs = [n for n in zf.namelist() if n.startswith("outputs/")]
        assert hashlib.sha256(manifest).hexdigest() == resp.headers["x-manifest-sha256"]
        assert len(outputs) == (render["file_count"] if mode == "embed" else 0)
        assert resp.headers["content-disposition"] == (
            f'attachment; filename="render-{render["id"]}-{mode}.zip"'
        )
        (tmp_path / f"{mode}.zip").write_bytes(resp.content)
    code, out = _cli(tmp_path / "embed.zip")
    assert code == 0, out
    assert f"{render['file_count']} output files re-hashed" in out
    # reference mode: the expert supplies the outputs (here, fetched one by one through the API)
    supplied: list[str | Path] = []
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for f in files["items"]:
            path = tmp_path / f["name"]
            path.write_bytes(
                (await c.get(f"/v1/renders/{render['id']}/files/{f['ord']}/content")).content
            )
            supplied += ["--file", path]
    code, out = _cli(tmp_path / "reference.zip", *supplied)
    assert code == 0, out

    reads = await _package_events(api, tenant)
    assert len(reads) == 4
    for e, resp in zip(reads, [*embed, *reference], strict=True):
        assert e.payload["manifest_sha256"] == resp.headers["x-manifest-sha256"]
        assert e.payload["render_id"] == render["id"] and e.payload["request_id"]


async def test_the_read_is_audited_and_anchored_before_the_first_byte(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    """Driven at the ASGI level: the state is read at the moment the first body byte is sent."""
    _, render = await _rendered(api, tenant)
    app = create_app(api.settings, api.resources)
    host = f"{tenant.subdomain}.{api.settings.api_base_domain}"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": f"/v1/renders/{render['id']}/package",
        "raw_path": f"/v1/renders/{render['id']}/package".encode(), "root_path": "",
        "query_string": b"outputs=embed", "client": ("127.0.0.1", 50000), "server": (host, 80),
        "headers": [(b"host", host.encode()),
                    (b"authorization", f"Bearer {tenant.token(api.settings)}".encode())],
    }  # fmt: skip
    at_first_byte: dict[str, Any] = {}
    status: list[int] = []

    requested = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if requested.is_set():  # the client stays connected until the response ends
            await asyncio.Event().wait()
        requested.set()
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])
        elif message.get("body") and not at_first_byte:
            reads = await _package_events(api, tenant)
            head, anchored = await _anchored_seq(api, tenant)
            at_first_byte.update(reads=reads, head=head, anchored=anchored)

    await app(scope, receive, send)
    assert status == [200]
    assert [e.event_type for e in at_first_byte["reads"]] == ["audit.render_package_read"]
    # the read is the newest audit event, and the anchor covers it
    assert at_first_byte["anchored"] == at_first_byte["head"]


# ------------------------------------------------------------------ abort
async def test_an_object_that_differs_from_its_record_aborts_the_stream(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, render = await _rendered(api, tenant)
    conn = await superuser(api.settings)
    try:  # the registry's record of the render's seal anchor no longer matches the WORM bytes
        await conn.execute(
            "UPDATE evidence_objects SET sha256 = $1, source_sha256 = $1 WHERE storage_key = $2",
            "ab" * 32,
            render["seal_storage_key"],
        )
    finally:
        await conn.close()
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        with pytest.raises(PackageIntegrityError, match="objects/" + "ab" * 32):
            await c.get(f"/v1/renders/{render['id']}/package")
    events = await _package_events(api, tenant)
    assert [e.event_type for e in events] == [
        "audit.render_package_read", "audit.render_package_aborted",
    ]  # fmt: skip
    aborted = events[1].payload
    assert aborted["integrity"] is True and aborted["entry"] == "objects/" + "ab" * 32
    assert aborted["manifest_sha256"] == events[0].payload["manifest_sha256"]
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        alerts = (
            await s.execute(
                text("SELECT kind, message FROM alerts WHERE kind = 'render_package_mismatch'")
            )
        ).all()
    assert len(alerts) == 1 and render["id"] in alerts[0].message
    head, anchored = await _anchored_seq(api, tenant)
    assert anchored == head  # the abort is anchored too
