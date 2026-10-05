"""Render package export -> ``edisc-verify`` offline (ADR 0015 §14, §19), and attacks on an exported
package: the render's custody stream, its reference to the job's seal, every file batch, every object
and every output file. The same package as a zip (the download stream): verified in place, identical
to the directory, byte-identical across runs, and opened by Info-ZIP, 7-Zip and macOS ``ditto``."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Literal

import pytest
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_custody.chain import compute_event_hash
from edisc_custody.export import export_package
from edisc_custody.render_export import (
    PackageIntegrityError,
    export_render_package,
    package_members,
    plan_render_package,
)
from edisc_custody.zipwriter import zip_stream
from edisc_worker.renders import RenderRun

from ..custody.conftest import superuser
from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..renders.conftest import drive, new_render
from ..renders.test_render_store import _job


def _cli(*args: str | Path) -> tuple[int, str]:
    """The verifier exactly as an expert runs it: a separate process with no DB/S3 settings."""
    proc = subprocess.run(
        [sys.executable, "-m", "edisc_custody.cli", *map(str, args)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=120,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _read(pkg: Path, name: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in (pkg / name).read_text().splitlines()]


def _write(pkg: Path, name: str, rows: list[dict[str, object]]) -> None:
    """A careful attacker rewrites the file AND the manifest entry for it."""
    data = b"".join(canonical_json(r) + b"\n" for r in rows)
    (pkg / name).write_bytes(data)
    manifest = json.loads((pkg / "manifest.json").read_bytes())
    manifest["files"][name] = {
        "sha256": hashlib.sha256(data).hexdigest(), "lines": len(rows), "bytes": len(data),
    }  # fmt: skip
    (pkg / "manifest.json").write_bytes(canonical_json(manifest))


class Rendered:
    def __init__(self, t: Tenant, job_id: uuid.UUID, render_id: uuid.UUID, root: Path) -> None:
        self.t, self.job_id, self.render_id, self.root = t, job_id, render_id, root


@pytest.fixture(scope="module")
async def rendered(
    app_sessions: Sessions,
    s3: S3Client,
    settings: Settings,
    tmp_path_factory: pytest.TempPathFactory,
) -> Rendered:
    rs = settings.model_copy(update={"render_files_batch_size": 2})
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    assert (await drive(RenderRun(app_sessions, s3, rs), t.tenant_id, render_id))[
        "status"
    ] == "completed"
    root = tmp_path_factory.mktemp("render-packages")
    await export_render_package(
        app_sessions, s3, rs, tenant_id=t.tenant_id, render_id=render_id, dest=root / "embedded"
    )
    await export_render_package(
        app_sessions, s3, rs, tenant_id=t.tenant_id, render_id=render_id, dest=root / "referenced",
        outputs="reference",
    )  # fmt: skip
    await export_package(
        app_sessions, s3, rs, tenant_id=t.tenant_id, job_id=job_id, dest=root / "job"
    )
    for mode in ("embed", "reference"):
        (root / f"{mode}.zip").write_bytes(
            await package_zip(app_sessions, s3, rs, t.tenant_id, render_id, mode)  # type: ignore[arg-type]
        )
    return Rendered(t, job_id, render_id, root)


async def package_zip(
    sessions: Sessions, s3: S3Client, settings: Settings, tenant_id: uuid.UUID,
    render_id: uuid.UUID, outputs: Literal["embed", "reference"],
) -> bytes:  # fmt: skip
    """The download stream, exactly as the API produces it."""
    plan = await plan_render_package(
        sessions, s3, settings, tenant_id=tenant_id, render_id=render_id, outputs=outputs
    )
    return b"".join([c async for c in zip_stream(package_members(sessions, s3, settings, plan))])


@pytest.fixture
def pkg(rendered: Rendered, tmp_path: Path) -> Path:
    """A fresh copy of the embedded package, for one attack."""
    return Path(shutil.copytree(rendered.root / "embedded", tmp_path / "pkg"))


async def test_a_clean_render_package_verifies_offline(rendered: Rendered) -> None:
    files = len(_read(rendered.root / "embedded", "files.jsonl"))
    code, out = _cli(rendered.root / "embedded")
    assert code == 0, out
    assert "VERIFIED  render package" in out
    assert f"{files} files in Merkle roots" in out and f"{files} output files re-hashed" in out
    code, out = _cli(rendered.root / "embedded", "--job-package", rendered.root / "job")
    assert code == 0, out
    assert "job package: VERIFIED" in out


async def test_outputs_referenced_by_hash_must_be_supplied(rendered: Rendered) -> None:
    code, out = _cli(rendered.root / "referenced")
    assert code == 1 and "not in the package and not supplied" in out
    supplied = sorted((rendered.root / "embedded" / "outputs").iterdir())
    args: list[str | Path] = [rendered.root / "referenced"]
    for p in supplied:
        args += ["--file", p]
    code, out = _cli(*args)
    assert code == 0, out


async def test_an_altered_output_byte_fails(pkg: Path) -> None:
    out_file = sorted((pkg / "outputs").iterdir())[0]
    data = bytearray(out_file.read_bytes())
    data[len(data) // 2] ^= 0x01
    out_file.write_bytes(bytes(data))
    code, out = _cli(pkg)
    assert code == 1 and "bytes do not match the recorded sha256/size" in out


async def test_a_missing_or_an_extra_output_fails(pkg: Path) -> None:
    victim = sorted((pkg / "outputs").iterdir())[0]
    (pkg / "outputs" / "planted.rsmf").write_bytes(victim.read_bytes())
    victim.unlink()
    code, out = _cli(pkg)
    assert code == 1
    assert f"output {victim.name}: not in the package" in out
    assert "outputs/planted.rsmf: not an output file of this render" in out


async def test_an_altered_file_record_breaks_its_batch_root(pkg: Path) -> None:
    files = _read(pkg, "files.jsonl")
    files[1]["record"]["event_count"] = 1  # type: ignore[index]
    _write(pkg, "files.jsonl", files)
    code, out = _cli(pkg)
    assert code == 1 and "render batch Merkle root mismatch" in out


async def test_a_dropped_file_record_is_caught(pkg: Path) -> None:
    files = _read(pkg, "files.jsonl")
    _write(pkg, "files.jsonl", files[:-1])
    code, out = _cli(pkg)
    assert code == 1 and "file_count" in out


async def test_a_swapped_seal_reference_is_caught(pkg: Path, rendered: Rendered) -> None:
    """An attacker points the package at another anchor of the job (an earlier, valid one)."""
    seal = json.loads((pkg / "job_seal.json").read_bytes())
    anchors = sorted(_read(rendered.root / "job", "anchors.jsonl"), key=lambda a: str(a["key"]))
    earlier = next(a for a in anchors if a["key"] != seal["key"])
    body = base64.b64decode(str(earlier["body_b64"]))
    sha = hashlib.sha256(body).hexdigest()
    (pkg / "objects" / seal["sha256"]).unlink()
    (pkg / "objects" / sha).write_bytes(body)
    _write(pkg, "job_seal.json", [{"key": earlier["key"], "version_id": earlier["version_id"],
                                   "sha256": sha, "size": len(body)}])  # fmt: skip
    code, out = _cli(pkg)
    assert code == 1 and "is not the seal anchor render_started references" in out


async def test_a_forged_seal_body_is_caught(pkg: Path) -> None:
    seal = json.loads((pkg / "job_seal.json").read_bytes())
    original = pkg / "objects" / seal["sha256"]
    doc = json.loads(original.read_bytes())
    original.unlink()  # a careful attacker leaves nothing behind
    doc["event_hash"] = "ab" * 32
    body = canonical_json(doc)
    seal["sha256"], seal["size"] = hashlib.sha256(body).hexdigest(), len(body)
    (pkg / "objects" / seal["sha256"]).write_bytes(body)
    _write(pkg, "job_seal.json", [seal])
    code, out = _cli(pkg)
    assert code == 1 and "does not anchor the head render_started references" in out


async def test_a_consistent_rewrite_of_the_render_stream_is_caught_by_its_anchors(
    pkg: Path,
) -> None:
    events = _read(pkg, "events.jsonl")
    events[0]["fields"]["payload"]["options"]["include_context"] = False  # type: ignore[index]
    prev = str(events[0]["prev_hash"])
    for ev in events:
        ev["prev_hash"] = prev
        ev["event_hash"] = prev = compute_event_hash(prev, ev["fields"])  # type: ignore[arg-type]
    _write(pkg, "events.jsonl", events)
    manifest = json.loads((pkg / "manifest.json").read_bytes())
    manifest["head"]["hash"] = prev
    (pkg / "manifest.json").write_bytes(canonical_json(manifest))
    code, out = _cli(pkg)
    assert code == 1 and "disagrees with the WORM anchor (chain rewritten)" in out


async def test_dropping_the_render_seal_fails(pkg: Path) -> None:
    anchors = sorted(_read(pkg, "anchors.jsonl"), key=lambda a: str(a["key"]))
    _write(pkg, "anchors.jsonl", anchors[:-1])
    code, out = _cli(pkg)
    assert code == 1 and "no matching WORM seal" in out


async def test_an_edit_without_fixing_the_manifest_fails(pkg: Path) -> None:
    (pkg / "files.jsonl").write_bytes((pkg / "files.jsonl").read_bytes() + b"\n")
    code, out = _cli(pkg)
    assert code == 1 and "does not match manifest" in out


async def test_a_same_length_edit_without_fixing_the_manifest_fails(pkg: Path) -> None:
    data = (pkg / "files.jsonl").read_bytes()
    (pkg / "files.jsonl").write_bytes(data.replace(b'"part":', b'"Part":', 1))  # same lines, size
    code, out = _cli(pkg)
    assert code == 1 and "files.jsonl: does not match manifest" in out


async def test_a_job_package_of_another_job_is_rejected(
    rendered: Rendered, app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path
) -> None:
    other = await _job(app_sessions, s3, settings, rendered.t, epoch=0)
    await export_package(
        app_sessions, s3, settings, tenant_id=rendered.t.tenant_id, job_id=other,
        dest=tmp_path / "other",
    )  # fmt: skip
    code, out = _cli(rendered.root / "embedded", "--job-package", tmp_path / "other")
    assert code == 1 and "belongs to another job" in out


# ------------------------------------------------------------------ format /2: objects
async def test_an_altered_anchor_object_fails(pkg: Path) -> None:
    anchor = _read(pkg, "anchors.jsonl")[0]
    path = pkg / "objects" / str(anchor["sha256"])
    path.write_bytes(path.read_bytes().replace(b'"seq":', b'"seq": '))
    code, out = _cli(pkg)
    assert (
        code == 1
        and f"objects/{anchor['sha256']}: bytes do not match the recorded sha256/size" in out
    )


async def test_a_missing_object_or_a_planted_file_fails(pkg: Path) -> None:
    seal = json.loads((pkg / "job_seal.json").read_bytes())
    (pkg / "objects" / seal["sha256"]).unlink()
    (pkg / "objects" / ("0" * 64)).write_bytes(b"{}")
    (pkg / "notes.txt").write_bytes(b"planted")
    code, out = _cli(pkg)
    assert code == 1
    assert f"job_seal.json: its object objects/{seal['sha256']} is not in the package" in out
    assert f"objects/{'0' * 64}: not part of this package" in out
    assert "notes.txt: not part of this package" in out


async def test_format_1_packages_still_verify(pkg: Path) -> None:
    """Directories exported before §19 carry the anchor and seal bodies inline."""

    def inline(rec: dict[str, object]) -> dict[str, object]:
        if rec.get("delete_marker"):
            return rec
        body = (pkg / "objects" / str(rec["sha256"])).read_bytes()
        return {"key": rec["key"], "version_id": rec["version_id"],
                "body_b64": base64.b64encode(body).decode()}  # fmt: skip

    anchors = [inline(a) for a in _read(pkg, "anchors.jsonl")]
    seal = [inline(r) for r in _read(pkg, "job_seal.json")]
    shutil.rmtree(pkg / "objects")
    _write(pkg, "anchors.jsonl", anchors)
    _write(pkg, "job_seal.json", seal)
    manifest = json.loads((pkg / "manifest.json").read_bytes())
    manifest["format"] = "edisc-render-package/1"
    (pkg / "manifest.json").write_bytes(canonical_json(manifest))
    code, out = _cli(pkg)
    assert code == 0, out


# ------------------------------------------------------------------ the zip
def _unzipped(data: bytes) -> dict[str, bytes]:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        return {i.filename: zf.read(i) for i in zf.infolist()}


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


async def test_the_zip_verifies_in_place_and_equals_the_directory(rendered: Rendered) -> None:
    for mode, directory in (("embed", "embedded"), ("reference", "referenced")):
        data = (rendered.root / f"{mode}.zip").read_bytes()
        assert _unzipped(data) == _tree(rendered.root / directory), mode
    code, out = _cli(rendered.root / "embed.zip", "--job-package", rendered.root / "job")
    assert code == 0, out
    assert "VERIFIED  render package" in out and "job package: VERIFIED" in out
    code, out = _cli(rendered.root / "reference.zip")
    assert code == 1 and "not in the package and not supplied" in out
    args: list[str | Path] = [rendered.root / "reference.zip"]
    for p in sorted((rendered.root / "embedded" / "outputs").iterdir()):
        args += ["--file", p]
    code, out = _cli(*args)
    assert code == 0, out
    names = list(_unzipped((rendered.root / "embed.zip").read_bytes()))
    assert names[:5] == ["manifest.json", "events.jsonl", "files.jsonl", "anchors.jsonl",
                         "job_seal.json"]  # fmt: skip


async def test_two_downloads_are_byte_identical(
    rendered: Rendered, app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    for mode in ("embed", "reference"):
        again = await package_zip(
            app_sessions,
            s3,
            settings,
            rendered.t.tenant_id,
            rendered.render_id,
            mode,  # type: ignore[arg-type]
        )
        assert again == (rendered.root / f"{mode}.zip").read_bytes(), mode
    manifest = json.loads(_unzipped(again)["manifest.json"])
    assert "exported_at" not in manifest and manifest["sealed_at"]


async def test_a_flipped_byte_inside_the_zip_fails(rendered: Rendered, tmp_path: Path) -> None:
    data = bytearray((rendered.root / "embed.zip").read_bytes())
    name = sorted((rendered.root / "embedded" / "outputs").iterdir())[0].name
    at = data.index(f"outputs/{name}".encode()) + len(f"outputs/{name}") + 40
    data[at] ^= 0x01
    (tmp_path / "bad.zip").write_bytes(bytes(data))
    code, out = _cli(tmp_path / "bad.zip")
    assert code == 1 and f"output {name}: crc_mismatch" in out


async def test_a_consistently_rezipped_output_fails(rendered: Rendered, tmp_path: Path) -> None:
    """An attacker rebuilds the zip with one output changed (valid CRCs): the recorded hash catches it."""
    import zipfile

    entries = _unzipped((rendered.root / "embed.zip").read_bytes())
    victim = next(n for n in entries if n.startswith("outputs/"))
    entries[victim] = entries[victim].replace(b"Subject:", b"Subject: x", 1)
    with zipfile.ZipFile(tmp_path / "rezipped.zip", "w") as zf:
        for n, d in entries.items():
            zf.writestr(n, d)
    code, out = _cli(tmp_path / "rezipped.zip")
    assert code == 1 and "bytes do not match the recorded sha256/size" in out


def _tool(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if os.environ.get("CI"):
        pytest.fail(f"none of {names} installed on the CI runner")
    pytest.skip(f"none of {names} installed")


@pytest.mark.parametrize("unzipper", ["unzip", "7z", "ditto"])
async def test_other_unzippers_extract_a_package_that_verifies(
    rendered: Rendered, tmp_path: Path, unzipper: str
) -> None:
    zipped = rendered.root / "embed.zip"
    out_dir = tmp_path / "x"
    if unzipper == "unzip":
        cmd = [_tool("unzip"), "-q", str(zipped), "-d", str(out_dir)]
    elif unzipper == "7z":
        cmd = [_tool("7zz", "7z"), "x", f"-o{out_dir}", str(zipped)]
    else:
        if sys.platform != "darwin":
            pytest.skip("ditto (the Archive Utility engine) is macOS only")
        cmd = ["ditto", "-x", "-k", str(zipped), str(out_dir)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)  # fmt: skip
    output, _ = await proc.communicate()
    assert proc.returncode == 0, output
    assert _tree(out_dir) == _tree(rendered.root / "embedded")
    code, out = _cli(out_dir)
    assert code == 0, out


# ------------------------------------------------------------------ pass 2 checks against the plan
async def _drain(sessions: Sessions, s3: S3Client, settings: Settings, plan: object) -> None:
    async for m in package_members(sessions, s3, settings, plan):  # type: ignore[arg-type]
        async for _ in m.chunks():
            pass


async def test_records_that_change_after_the_plan_abort_the_package(
    rendered: Rendered, app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    plan = await plan_render_package(
        app_sessions, s3, settings, tenant_id=rendered.t.tenant_id, render_id=rendered.render_id
    )
    conn = await superuser(settings)
    try:
        actor = await conn.fetchval(
            "SELECT actor FROM custody_events WHERE stream_id = $1 AND seq = 2", rendered.render_id
        )
        await conn.execute(
            "UPDATE custody_events SET actor = 'mallory' WHERE stream_id = $1 AND seq = 2",
            rendered.render_id,
        )
        with pytest.raises(
            PackageIntegrityError, match=r"events\.jsonl: differs from the manifest"
        ):
            await _drain(app_sessions, s3, settings, plan)
    finally:
        await conn.execute(
            "UPDATE custody_events SET actor = $2 WHERE stream_id = $1 AND seq = 2",
            rendered.render_id, actor,
        )  # fmt: skip
        await conn.close()
    await _drain(app_sessions, s3, settings, plan)  # restored: the same plan streams cleanly


async def test_an_output_whose_registry_disagrees_with_its_record_aborts_the_package(
    rendered: Rendered, app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    plan = await plan_render_package(
        app_sessions, s3, settings, tenant_id=rendered.t.tenant_id, render_id=rendered.render_id
    )
    conn = await superuser(settings)
    try:
        row = await conn.fetchrow(
            "SELECT e.id, e.version_id FROM render_files rf JOIN evidence_objects e"
            " ON e.id = rf.evidence_object_id WHERE rf.render_id = $1 ORDER BY rf.ord LIMIT 1",
            rendered.render_id,
        )
        await conn.execute(
            "UPDATE evidence_objects SET version_id = 'shadow' WHERE id = $1", row["id"]
        )
        with pytest.raises(PackageIntegrityError, match="registry disagrees"):
            await _drain(app_sessions, s3, settings, plan)
    finally:
        await conn.execute(
            "UPDATE evidence_objects SET version_id = $2 WHERE id = $1",
            row["id"],
            row["version_id"],
        )
        await conn.close()
