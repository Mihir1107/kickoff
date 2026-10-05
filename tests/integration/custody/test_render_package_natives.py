"""Attacks on a render package that carries natives (``edisc-render-package/3``, ADR 0015 §20.6): a
native altered, missing or unlisted; a native nothing references; an ``.rsmf`` that names a native
the records do not list; a stray ``natives/`` file. Each is caught by ``edisc-verify``, run as an
expert runs it, with the careful attacker who also rewrites the manifest entry of a JSONL file."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.render_export import export_render_package
from edisc_worker.renders import RenderRun

from ..normalizer.harness import Sessions, new_tenant
from ..renders.conftest import drive, new_render
from ..renders.test_render_natives import BIG, OPTIONS
from ..renders.test_render_store import _job
from .test_render_package import _cli, _read, _write


@pytest.fixture(scope="module")
async def exported(
    app_sessions: Sessions, s3: S3Client, settings: Settings,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:  # fmt: skip
    rs = settings.model_copy(update={"render_files_batch_size": 2})
    t = await new_tenant(app_sessions)
    job_id = await _job(app_sessions, s3, rs, t, epoch=0, spec=BIG)
    render_id = await new_render(app_sessions, t.tenant_id, job_id, OPTIONS)
    assert (await drive(RenderRun(app_sessions, s3, rs), t.tenant_id, render_id))[
        "status"
    ] == "completed"
    root = tmp_path_factory.mktemp("native-packages")
    await export_render_package(
        app_sessions, s3, rs, tenant_id=t.tenant_id, render_id=render_id, dest=root / "embedded"
    )
    return root / "embedded"


@pytest.fixture
def pkg(exported: Path, tmp_path: Path) -> Path:
    return Path(shutil.copytree(exported, tmp_path / "pkg"))


def _natives(pkg: Path) -> list[dict[str, object]]:
    return _read(pkg, "natives.jsonl")


def _fails(pkg: Path, *expected: str) -> str:
    code, out = _cli(pkg)
    assert code == 1, out
    for e in expected:
        assert e in out, out
    return out


def test_the_clean_package_verifies(exported: Path) -> None:
    natives = _natives(exported)
    assert natives
    code, out = _cli(exported)
    assert code == 0, out
    assert f"{len(natives)} natives re-hashed" in out


def test_an_altered_native_is_caught(pkg: Path) -> None:
    sha = str(_natives(pkg)[0]["record"]["sha256"])  # type: ignore[index]
    path = pkg / "natives" / sha
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 1
    path.write_bytes(bytes(data))
    _fails(pkg, f"native {sha}: bytes do not match")


def test_a_missing_native_is_caught(pkg: Path) -> None:
    sha = str(_natives(pkg)[0]["record"]["sha256"])  # type: ignore[index]
    (pkg / "natives" / sha).unlink()
    _fails(pkg, f"native {sha}: not in the package and not supplied")


def test_a_native_dropped_from_the_records_is_caught(pkg: Path) -> None:
    """The `.rsmf` still names it: an unlisted native, and the batch's natives_root no longer holds."""
    lines = _natives(pkg)
    sha = str(lines[-1]["record"]["sha256"])  # type: ignore[index]
    _write(pkg, "natives.jsonl", lines[:-1])
    (pkg / "natives" / sha).unlink()
    _fails(pkg, "which is not listed", "natives_root")


def test_an_unreferenced_native_is_caught(pkg: Path) -> None:
    """A native record (and object) added for a file that does not reference it."""
    lines = _natives(pkg)
    data = b"a native nothing references"
    sha = hashlib.sha256(data).hexdigest()
    extra = json.loads(json.dumps(lines[-1]))
    extra["record"].update({"ord": len(lines), "sha256": sha, "size": len(data)})
    _write(pkg, "natives.jsonl", [*lines, extra])
    (pkg / "natives" / sha).write_bytes(data)
    _fails(pkg, "the records list", "natives_root")


def test_a_rewritten_reference_to_another_file_is_caught(pkg: Path) -> None:
    """The records claim a native belongs to other files than the ones whose `.rsmf` names it."""
    lines = _natives(pkg)
    rec = lines[0]["record"]
    rec["file_ords"] = [o + 1 for o in rec["file_ords"]]  # type: ignore[index, union-attr]
    _write(pkg, "natives.jsonl", lines)
    _fails(pkg, "the records list")


def test_a_stray_native_file_is_caught(pkg: Path) -> None:
    (pkg / "natives" / ("0" * 64)).write_bytes(b"stray")
    _fails(pkg, "not a native of this render")
