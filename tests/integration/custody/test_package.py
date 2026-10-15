"""Export -> edisc-verify offline, and the same attacks performed on an exported package."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_custody.chain import compute_event_hash
from edisc_custody.export import export_package

from .conftest import run_job

Sessions = async_sessionmaker[AsyncSession]


def _cli_blocking(pkg: Path, *args: str) -> tuple[int, str]:
    """Run the verifier exactly as an expert would: separate process, no DB/S3 settings in its env."""
    proc = subprocess.run(
        [sys.executable, "-m", "edisc_custody.cli", str(pkg), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=120,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _read(pkg: Path, name: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in (pkg / name).read_text().splitlines()]


def _write(pkg: Path, name: str, rows: list[dict[str, object]], *, fix_manifest: bool) -> None:
    data = b"".join(canonical_json(r) + b"\n" for r in rows)
    (pkg / name).write_bytes(data)
    if fix_manifest:  # a careful attacker also rewrites the manifest
        manifest = json.loads((pkg / "manifest.json").read_bytes())
        manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "lines": len(rows)}
        (pkg / "manifest.json").write_bytes(canonical_json(manifest))


@pytest.fixture
async def package(app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path) -> Path:
    job = await run_job(app_sessions, s3, settings, batches=12, items_per_batch=4)
    return await export_package(
        app_sessions,
        s3,
        settings,
        tenant_id=job.tenant_id,
        job_id=job.job_id,
        dest=tmp_path / "pkg",
    )


async def test_clean_package_verifies_offline(package: Path) -> None:
    code, out = await _cli(package)
    assert code == 0, out
    assert "VERIFIED" in out
    assert "12 batches" in out
    assert "48 items and 12 evidence objects re-hashed" in out


def test_verifier_imports_no_database_or_cloud_code() -> None:
    code = (
        "import sys, edisc_custody.cli;"
        "bad = [m for m in ('sqlalchemy', 'asyncpg', 'aiobotocore', 'botocore', 'edisc_db', 'edisc_evidence')"
        " if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout


async def test_edit_without_fixing_manifest(package: Path) -> None:
    events = _read(package, "events.jsonl")
    events[2]["fields"]["actor"] = "mallory"  # type: ignore[index]
    _write(package, "events.jsonl", events, fix_manifest=False)
    code, out = await _cli(package)
    assert code == 1
    assert "does not match manifest" in out


async def test_consistent_offline_rewrite_is_caught_by_anchors(package: Path) -> None:
    events = _read(package, "events.jsonl")
    events[2]["fields"]["payload"] = {"note": "forged"}  # type: ignore[index]
    prev = str(events[2]["prev_hash"])
    for ev in events[2:]:
        ev["prev_hash"] = prev
        ev["event_hash"] = prev = compute_event_hash(prev, ev["fields"])  # type: ignore[arg-type]
    _write(package, "events.jsonl", events, fix_manifest=True)
    manifest = json.loads((package / "manifest.json").read_bytes())
    manifest["head"]["hash"] = prev
    (package / "manifest.json").write_bytes(canonical_json(manifest))
    code, out = await _cli(package)
    assert code == 1
    assert "disagrees with the WORM anchor (chain rewritten)" in out


async def test_item_content_hash_change_breaks_merkle_and_key(package: Path) -> None:
    items = _read(package, "items.jsonl")
    items[5]["content_hash"] = "ee" * 32
    _write(package, "items.jsonl", items, fix_manifest=True)
    code, out = await _cli(package)
    assert code == 1
    assert "Merkle root mismatch" in out
    assert "idempotency_key does not match" in out


async def test_evidence_bytes_altered(package: Path) -> None:
    obj = next((package / "objects").iterdir())
    obj.write_bytes(obj.read_bytes().replace(b"msg", b"MSG", 1))
    code, out = await _cli(package)
    assert code == 1
    assert "bytes do not match recorded sha256" in out


async def test_raw_hash_checked_against_page_fragment(package: Path) -> None:
    items = _read(package, "items.jsonl")
    items[0]["json_path"] = (
        "$.messages[1]" if items[0]["json_path"] != "$.messages[1]" else "$.messages[0]"
    )
    _write(package, "items.jsonl", items, fix_manifest=True)
    code, out = await _cli(package)
    assert code == 1
    assert "raw_hash does not match the page fragment" in out


async def test_dropping_the_seal_anchor_fails_a_finalized_package(package: Path) -> None:
    anchors = _read(package, "anchors.jsonl")
    _write(
        package,
        "anchors.jsonl",
        sorted(anchors, key=lambda a: str(a["key"]))[:-1],
        fix_manifest=True,
    )
    code, out = await _cli(package)
    assert code == 1
    assert "no matching WORM seal" in out


def test_unreadable_package_exits_2(tmp_path: Path) -> None:
    code, out = _cli_blocking(tmp_path)
    assert code == 2, out


# ------------------------------------------------------------------ strict, and zips read in place
NO_WRITES = """
import os, sys
from edisc_custody.cli import main
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def hook(event, args):
    if event == "open" and (
        (isinstance(args[1], str) and any(c in args[1] for c in "wax+")) or (args[2] & WRITE)
    ):
        raise PermissionError(f"the verifier wrote: {args[0]!r}")
    if event in ("os.mkdir", "os.rename", "os.remove", "os.link", "os.symlink", "shutil.copyfile",
                 "tempfile.mkstemp", "tempfile.mkdtemp"):
        raise PermissionError(f"the verifier changed the filesystem: {event} {args!r}")
sys.addaudithook(hook)
sys.exit(main(sys.argv[1:]))
"""


def cli_without_writes_blocking(*args: str | Path) -> tuple[int, str]:
    """The verifier with an audit hook that fails on any file write, directory or temp file: a zip
    package is read where it lies, never copied out (ADR 0018 §28)."""
    proc = subprocess.run(
        [sys.executable, "-B", "-c", NO_WRITES, *map(str, args)],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=120, check=False,
    )  # fmt: skip
    return proc.returncode, proc.stdout + proc.stderr


def _hook_control(target: Path) -> tuple[int, str]:
    script = NO_WRITES.replace("sys.exit(main(sys.argv[1:]))", "open(sys.argv[1], 'wb').close()")
    proc = subprocess.run(
        [sys.executable, "-B", "-c", script, str(target)],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    return proc.returncode, proc.stdout + proc.stderr


def zip_directory(src: Path, dest: Path, *, extra: dict[str, bytes] | None = None,
                  method: int = zipfile.ZIP_STORED) -> Path:  # fmt: skip
    with zipfile.ZipFile(dest, "w", compression=method) as zf:
        for p in sorted(q for q in src.rglob("*") if q.is_file()):
            zf.write(p, p.relative_to(src).as_posix())
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return dest


async def test_a_file_the_manifest_does_not_list_fails_the_package(package: Path) -> None:
    (package / "notes.txt").write_text("added after export")
    code, out = await _cli(package)
    assert code == 1 and "notes.txt: not part of this package" in out, out
    (package / "notes.txt").unlink()
    (package / "objects" / ("ab" * 32)).write_bytes(b"an object nothing references")
    code, out = await _cli(package)
    assert code == 1 and f"objects/{'ab' * 32}: not an evidence object" in out, out


async def test_os_metadata_is_tolerated_only_in_a_directory_and_only_on_request(
    package: Path, tmp_path: Path
) -> None:
    (package / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
    (package / "objects" / "._x").write_bytes(b"AppleDouble")
    code, out = await _cli(package)
    assert code == 1 and ".DS_Store: not part of this package" in out, out
    code, out = await _cli(package, "--tolerate-os-metadata")
    assert code == 0, out
    assert "TOLERATED .DS_Store" in out and "TOLERATED objects/._x" in out
    # a zip is verified strictly, whatever the option says
    (package / ".DS_Store").unlink()
    (package / "objects" / "._x").unlink()
    pkg_zip = zip_directory(package, tmp_path / "job.zip", extra={".DS_Store": b"x"})
    code, out = await _cli(pkg_zip, "--tolerate-os-metadata")
    assert code == 1 and ".DS_Store: not part of this package" in out, out
    assert "does not apply to a zip" in out


async def test_a_zip_package_is_verified_in_place(package: Path, tmp_path: Path) -> None:
    pkg_zip = zip_directory(package, tmp_path / "job.zip")
    code, out = await cli_without_writes(pkg_zip)
    assert code == 0, out
    assert "48 items and 12 evidence objects re-hashed" in out
    # the control: the hook does catch a write (so the check above can fail)
    code, out = _hook_control(tmp_path / "x")
    assert code != 0 and "the verifier wrote" in out
    # tampered inside the zip: caught by the same checks
    data = bytearray(pkg_zip.read_bytes())
    at = data.find(b'"actor":"')
    data[at + 9] ^= 0x01
    (tmp_path / "bad.zip").write_bytes(bytes(data))
    code, out = await cli_without_writes(tmp_path / "bad.zip")
    assert code in (1, 2) and not out.startswith("VERIFIED"), out


async def _cli(*args: Any, **kwargs: Any) -> Any:
    """``_cli_blocking`` off the event loop: the CLI runs for seconds and the loop guard fails
    a test whose loop waits on it (a synchronous subprocess wait on the loop thread)."""
    return await asyncio.to_thread(_cli_blocking, *args, **kwargs)


async def cli_without_writes(*args: Any, **kwargs: Any) -> Any:
    """``cli_without_writes_blocking`` off the event loop: the CLI runs for seconds and the loop guard fails
    a test whose loop waits on it (a synchronous subprocess wait on the loop thread)."""
    return await asyncio.to_thread(cli_without_writes_blocking, *args, **kwargs)
