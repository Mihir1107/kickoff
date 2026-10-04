"""Render package export -> ``edisc-verify`` offline (ADR 0015 §14), and attacks on an exported package:
the render's custody stream, its reference to the job's seal, every file batch and every output file."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_custody.chain import compute_event_hash
from edisc_custody.export import export_package
from edisc_custody.render_export import export_render_package
from edisc_worker.renders import RenderRun

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
    manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "lines": len(rows)}
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
    return Rendered(t, job_id, render_id, root)


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
    _write(pkg, "job_seal.json", [{k: earlier[k] for k in ("key", "version_id", "body_b64")}])
    code, out = _cli(pkg)
    assert code == 1 and "is not the seal anchor render_started references" in out


async def test_a_forged_seal_body_is_caught(pkg: Path) -> None:
    seal = json.loads((pkg / "job_seal.json").read_bytes())
    doc = json.loads(base64.b64decode(seal["body_b64"]))
    doc["event_hash"] = "ab" * 32
    seal["body_b64"] = base64.b64encode(canonical_json(doc)).decode()
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
