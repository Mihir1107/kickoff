"""The job custody package download (M16 step 5, ADR 0018 §27): ``GET /v1/jobs/{id}/package``, under
the render package's rules: ``export.read``, sealed jobs only, the audit committed AND anchored before
the first byte, a deterministic zip with an exact ``Content-Length`` that ``edisc-verify`` accepts as
downloaded, the same bytes as the directory export, and an aborted stream (audited, alerted) when an
object differs from its record."""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from edisc_custody.export import export_package
from edisc_custody.job_export import PackageIntegrityError
from edisc_db.session import tenant_tx

from ..custody.conftest import superuser
from .conftest import Api, TenantCtx, add_principal
from .cut import check_cut, cut_download, full_length
from .first_byte import first_byte
from .test_jobs import custody, make_world
from .test_reports import _sealed_job, report_worker

__all__ = ["report_worker"]  # the fixture, re-exported for pytest


def _cli_blocking(*args: str | Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "edisc_custody.cli", *map(str, args)],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=300, check=False,
    )  # fmt: skip
    return proc.returncode, proc.stdout + proc.stderr


async def _events(api: Api, t: TenantCtx) -> list[str]:
    return [
        e.event_type
        for e in await custody(api, t, str(t.tenant_id))
        if e.event_type in ("audit.job_package_read", "audit.job_package_aborted")
    ]


async def test_only_matter_managers_and_tenant_admins_download_job_packages(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    w, job_id = await _sealed_job(api, tenant)
    matter, client = uuid.UUID(w.matter), uuid.UUID(w.client)
    url = f"/v1/jobs/{job_id}/package"
    for role, scope, sid in [("reviewer", "matter", matter), ("auditor", "matter", matter),
                             ("collector", "matter", matter), ("client_admin", "client", client)]:  # fmt: skip
        _, subject = await add_principal(api, tenant, roles=[(role, scope, sid)])
        async with api.client(tenant.subdomain, tenant.token(api.settings, subject=subject)) as c:
            assert (await c.get(url)).status_code == 403, role
    _, manager = await add_principal(api, tenant, roles=[("matter_manager", "matter", matter)])
    async with api.client(tenant.subdomain, tenant.token(api.settings, subject=manager)) as c:
        got = await c.get(url)
        assert got.status_code == 200, got.text
        assert got.headers["content-type"] == "application/zip"
        assert (await c.get(url, params={"objects": "all"})).status_code == 422
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        assert (await c.get(f"/v1/jobs/{uuid.uuid4()}/package")).status_code == 404
    assert await _events(api, tenant) == ["audit.job_package_read"]  # refusals are not reads


async def test_a_job_that_is_not_sealed_gets_409(api: Api, tenant: TenantCtx) -> None:
    w = await make_world(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        job_id = (await c.post(f"/v1/matters/{w.matter}/jobs", json=w.job_body())).json()["id"]
        r = await c.get(f"/v1/jobs/{job_id}/package")
        assert (r.status_code, r.json()["error"]) == (409, "job_not_sealed")
    assert await _events(api, tenant) == []


async def test_downloads_verify_are_byte_identical_and_equal_the_directory_export(
    api: Api, tenant: TenantCtx, report_worker: None, tmp_path: Path
) -> None:
    _, job_id = await _sealed_job(api, tenant)
    url = f"/v1/jobs/{job_id}/package"
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        embed = [await c.get(url) for _ in range(2)]
        omit = await c.get(url, params={"objects": "omit"})
    assert embed[0].content == embed[1].content and embed[0].content != omit.content
    for resp in (embed[0], omit):
        assert int(resp.headers["content-length"]) == len(resp.content)
    (tmp_path / "job.zip").write_bytes(embed[0].content)
    code, out = await _cli(tmp_path / "job.zip")
    assert code == 0, out
    assert "evidence objects re-hashed" in out
    # one generator: the directory export holds exactly the zip's entries and bytes
    dest = await export_package(api.sessions, api.s3, api.settings, tenant_id=tenant.tenant_id,
                                job_id=uuid.UUID(job_id), dest=tmp_path / "dir",
                                archives="reference")  # fmt: skip
    with zipfile.ZipFile(io.BytesIO(embed[0].content)) as zf:
        for name in zf.namelist():
            assert (dest / name).read_bytes() == zf.read(name), name
        assert sorted(zf.namelist()) == sorted(
            str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()
        )
        manifest = zf.read("manifest.json")
    assert b'"format":"edisc-custody-package/3"' in manifest and b"exported_at" not in manifest


async def test_the_read_is_audited_and_anchored_before_the_first_byte(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    _, job_id = await _sealed_job(api, tenant)
    for mode in ("embed", "omit"):
        got = await first_byte(api, tenant, f"/v1/jobs/{job_id}/package", f"objects={mode}")
        assert got.status == 200
        assert got.newest.event_type == "audit.job_package_read", got.newest
        assert got.newest.payload["objects"] == mode
        assert got.anchored >= got.newest.seq


async def test_an_object_that_differs_from_its_record_aborts_the_stream(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    _, job_id = await _sealed_job(api, tenant)
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        key = (
            await s.execute(
                text(
                    "SELECT storage_key FROM evidence_objects WHERE job_id = :j AND kind = 'page'"
                    " ORDER BY storage_key LIMIT 1"
                ),
                {"j": uuid.UUID(job_id)},
            )
        ).scalar_one()
    conn = await superuser(api.settings)
    try:  # the registry's record of a page no longer matches the WORM bytes
        await conn.execute(
            "UPDATE evidence_objects SET sha256 = $1, source_sha256 = $1 WHERE storage_key = $2",
            "cd" * 32,
            key,
        )
    finally:
        await conn.close()
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        with pytest.raises(PackageIntegrityError, match="objects/" + "cd" * 32):
            await c.get(f"/v1/jobs/{job_id}/package")
    assert await _events(api, tenant) == ["audit.job_package_read", "audit.job_package_aborted"]
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        alerts = (
            (
                await s.execute(
                    text("SELECT message FROM alerts WHERE kind = 'job_package_mismatch'")
                )
            )
            .scalars()
            .all()
        )
    assert len(alerts) == 1 and job_id in alerts[0]


async def _cli(*args: Any, **kwargs: Any) -> Any:
    """``_cli_blocking`` off the event loop: the CLI runs for seconds and the loop guard fails
    a test whose loop waits on it (a synchronous subprocess wait on the loop thread)."""
    return await asyncio.to_thread(_cli_blocking, *args, **kwargs)


# ------------------------------------------------------------------ cut when the session ends (5b)
async def test_a_job_package_is_cut_when_the_session_is_revoked_and_is_no_zip(
    api: Api, tenant: TenantCtx, report_worker: None
) -> None:
    _, job_id = await _sealed_job(api, tenant)
    path = f"/v1/jobs/{job_id}/package"
    await full_length(api, tenant, path, "objects=embed")
    check_cut(await cut_download(api, tenant, path, "objects=embed"), "job_package", zip_file=True)
