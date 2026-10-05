"""Natives through the API (ADR 0015 §11, §20): the threshold in the render request, the list
(``custody.read``), the audited download (``export.read``, anchored before the first byte, re-hashed
while streaming, an aborted stream on a mismatch), and render packages carrying natives that
``edisc-verify`` accepts as downloaded, with an exact ``Content-Length``."""

from __future__ import annotations

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

from edisc_connector_dummy.dataset import Dataset
from edisc_custody.render_export import PackageIntegrityError
from edisc_db.session import tenant_tx

from ..custody.conftest import superuser
from ..worker.conftest import spec
from .conftest import Api, TenantCtx, add_principal
from .first_byte import first_byte
from .test_jobs import custody, make_world, wait_status
from .test_renders import render_worker, wait_render

__all__ = ["render_worker"]
MIB = 1 << 20
# every file between 2 MiB and 2 MiB + 64 KiB: all over a 1 MiB threshold, none over 3 MiB
BIG = spec(conversations=2, messages_per_unit=12, p_file=0.15, file_size_min=2 * MIB,
           file_size_span=64 << 10)  # fmt: skip


def _cli(*args: str | Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "edisc_custody.cli", *map(str, args)],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=120, check=False,
    )  # fmt: skip
    return proc.returncode, proc.stdout + proc.stderr


async def _rendered(
    api: Api, t: TenantCtx, threshold: int = MIB
) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    w = await make_world(api, t, BIG)
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    assert (await wait_status(api, t, job_id))["sealed"]
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        created = await c.post(
            f"/v1/jobs/{job_id}/renders", json={"external_over_bytes": threshold}
        )
        assert created.status_code == 201, created.text
        render = await wait_render(c, created.json()["id"], within=180)
        natives = (
            await c.get(f"/v1/renders/{render['id']}/natives", params={"limit": 200})
        ).json()["items"]
    assert (render["status"], render["sealed"]) == ("completed", True), render
    return w, render, natives


async def test_the_threshold_is_validated_and_part_of_the_identity(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    w = await make_world(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
    assert (await wait_status(api, tenant, job_id))["sealed"]
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for bad in (MIB - 1, (4 << 30) + 1, "2097152", 1.5e6, True):
            r = await c.post(f"/v1/jobs/{job_id}/renders", json={"external_over_bytes": bad})
            assert r.status_code == 422, (bad, r.text)
        a = await c.post(f"/v1/jobs/{job_id}/renders", json={})
        b = await c.post(f"/v1/jobs/{job_id}/renders", json={"external_over_bytes": 2 * MIB})
        assert (a.status_code, b.status_code) == (201, 201) and a.json()["id"] != b.json()["id"]
        assert a.json()["options"]["external_over_bytes"] == 1 << 30
        assert b.json()["options"]["external_over_bytes"] == 2 * MIB
        for r in (a, b):
            assert (await wait_render(c, r.json()["id"]))["status"] == "completed"


async def test_natives_are_listed_and_downloaded_with_the_collected_bytes(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    w, render, natives = await _rendered(api, tenant)
    assert natives, "the dataset has files over the threshold"
    assert render["summary"]["external_attachments"] >= len(natives)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        files = (await c.get(f"/v1/renders/{render['id']}/files", params={"limit": 200})).json()
    assert sum(f["external_count"] for f in files["items"]) >= len(natives)
    referenced = {o for n in natives for o in n["file_ords"]}
    assert referenced <= {f["ord"] for f in files["items"]}
    # the bytes are the source's: every native is a file of the dataset, with its hash
    ds = Dataset(BIG)
    by_sha = {}
    for conv in ds.conversations():
        for index in range(max(3, BIG.messages_per_unit // 8)):
            data = ds.file_bytes(ds._file(conv.id, index).id)
            by_sha[hashlib.sha256(data).hexdigest()] = data
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for n in natives:
            got = await c.get(f"/v1/renders/{render['id']}/natives/{n['sha256']}/content")
            assert got.status_code == 200, got.text
            assert got.content == by_sha[n["sha256"]] and len(got.content) == n["size"]
            assert got.headers["x-evidence-sha256"] == n["sha256"]
            assert int(got.headers["content-length"]) == n["size"]
        assert (
            await c.get(f"/v1/renders/{render['id']}/natives/{'0' * 64}/content")
        ).status_code == 404
        assert (
            await c.get(f"/v1/renders/{render['id']}/natives/not-a-hash/content")
        ).status_code == 422
    reads = [
        e
        for e in await custody(api, tenant, str(tenant.tenant_id))
        if e.event_type == "audit.render_native_read"
    ]
    assert [e.payload["sha256"] for e in reads] == [n["sha256"] for n in natives]


async def test_who_may_list_and_who_may_download(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    w, render, natives = await _rendered(api, tenant)
    matter = uuid.UUID(w.matter)
    listing = f"/v1/renders/{render['id']}/natives"
    content = f"{listing}/{natives[0]['sha256']}/content"
    for role, may_download in (("auditor", False), ("reviewer", False), ("matter_manager", True)):
        _, subject = await add_principal(api, tenant, roles=[(role, "matter", matter)])
        async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
            listed = await c.get(listing)
            downloaded = await c.get(content)
        if role != "reviewer":
            assert listed.status_code == 200, (role, listed.text)
        assert (downloaded.status_code == 200) == may_download, (role, downloaded.status_code)
        if not may_download:
            assert downloaded.status_code == 403, role
    other = await make_world(api, tenant)
    _, outsider = await add_principal(
        api, tenant, roles=[("matter_manager", "matter", uuid.UUID(other.matter))]
    )
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=outsider)) as c:
        assert (await c.get(listing)).status_code == 404
        assert (await c.get(content)).status_code == 404


async def test_a_native_read_is_anchored_before_the_first_byte(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, render, natives = await _rendered(api, tenant)
    sha = natives[0]["sha256"]
    got = await first_byte(api, tenant, f"/v1/renders/{render['id']}/natives/{sha}/content")
    assert got.status == 200
    assert got.newest.event_type == "audit.render_native_read", got.newest
    assert got.newest.payload["sha256"] == sha
    assert got.anchored >= got.newest.seq
    assert got.body_bytes == natives[0]["size"]


async def test_a_native_that_differs_from_its_record_aborts_the_download(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, render, natives = await _rendered(api, tenant)
    n = natives[0]
    conn = await superuser(api.settings)
    try:  # registry and render record rewritten together (triggers bypassed): the bytes disagree
        await conn.execute("SET session_replication_role = replica")
        await conn.execute(
            "UPDATE evidence_objects SET sha256 = $1, source_sha256 = $1 WHERE storage_key = $2",
            "ab" * 32, f"t/{tenant.tenant_id}/productions/{render['id']}/natives/sha256/{n['sha256']}",
        )  # fmt: skip
        await conn.execute(
            "UPDATE render_natives SET sha256 = $1, storage_key = $4 WHERE render_id = $2 AND ord = $3",
            "ab" * 32, uuid.UUID(render["id"]), n["ord"],
            f"t/{tenant.tenant_id}/productions/{render['id']}/natives/sha256/{'ab' * 32}",
        )  # fmt: skip
    finally:
        await conn.close()
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        with pytest.raises(RuntimeError, match="bytes differ from the record"):
            await c.get(f"/v1/renders/{render['id']}/natives/{'ab' * 32}/content")
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        alerts = (
            await s.execute(text("SELECT message FROM alerts WHERE kind = 'production_mismatch'"))
        ).all()
    assert len(alerts) == 1 and "native" in alerts[0].message


async def test_packages_with_natives_verify_as_downloaded(
    api: Api, tenant: TenantCtx, render_worker: None, tmp_path: Path
) -> None:
    _, render, natives = await _rendered(api, tenant)
    url = f"/v1/renders/{render['id']}/package"
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        embed = [await c.get(url, params={"outputs": "embed"}) for _ in range(2)]
        reference = await c.get(url)
        files = (await c.get(f"/v1/renders/{render['id']}/files", params={"limit": 200})).json()
    assert embed[0].content == embed[1].content  # byte identical, natives included
    for resp in (embed[0], reference):
        assert int(resp.headers["content-length"]) == len(resp.content)
    with zipfile.ZipFile(io.BytesIO(embed[0].content)) as zf:
        names = zf.namelist()
        native_lines = zf.read("natives.jsonl").splitlines()
    assert sorted(n for n in names if n.startswith("natives/")) == sorted(
        f"natives/{n['sha256']}" for n in natives
    )
    last_output = max(i for i, n in enumerate(names) if n.startswith("outputs/"))
    assert min(i for i, n in enumerate(names) if n.startswith("natives/")) > last_output
    assert len(native_lines) == len(natives)
    (tmp_path / "embed.zip").write_bytes(embed[0].content)
    code, out = _cli(tmp_path / "embed.zip")
    assert code == 0, out
    assert f"{len(natives)} natives re-hashed" in out

    # reference mode: outputs and natives supplied by the expert, matched by SHA-256
    (tmp_path / "reference.zip").write_bytes(reference.content)
    supplied: list[str | Path] = []
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        for f in files["items"]:
            path = tmp_path / f["name"]
            path.write_bytes(
                (await c.get(f"/v1/renders/{render['id']}/files/{f['ord']}/content")).content
            )
            supplied += ["--file", path]
        outputs_only = list(supplied)
        for n in natives:
            path = tmp_path / n["sha256"]
            path.write_bytes(
                (await c.get(f"/v1/renders/{render['id']}/natives/{n['sha256']}/content")).content
            )
            supplied += ["--file", path]
    code, out = _cli(tmp_path / "reference.zip", *supplied)
    assert code == 0, out
    code, out = _cli(tmp_path / "reference.zip", *outputs_only)
    assert code == 1 and "not in the package and not supplied" in out, out


async def test_a_native_that_differs_from_its_record_aborts_the_package(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, render, natives = await _rendered(api, tenant)
    key = f"t/{tenant.tenant_id}/productions/{render['id']}/natives/sha256/{natives[0]['sha256']}"
    conn = await superuser(api.settings)
    try:
        await conn.execute(
            "UPDATE evidence_objects SET sha256 = $1, source_sha256 = $1 WHERE storage_key = $2",
            "cd" * 32, key,
        )  # fmt: skip
    finally:
        await conn.close()
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        assert (await c.get(f"/v1/renders/{render['id']}/package")).status_code == 200  # reference
        with pytest.raises(PackageIntegrityError, match=f"natives/{natives[0]['sha256']}"):
            await c.get(f"/v1/renders/{render['id']}/package", params={"outputs": "embed"})
    aborted = [
        e
        for e in await custody(api, tenant, str(tenant.tenant_id))
        if e.event_type == "audit.render_package_aborted"
    ]
    assert len(aborted) == 1 and aborted[0].payload["integrity"] is True
