"""Export -> edisc-verify offline, and the same attacks performed on an exported package."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_custody.chain import compute_event_hash
from edisc_custody.export import export_package

from .conftest import run_job

Sessions = async_sessionmaker[AsyncSession]


def _cli(pkg: Path) -> tuple[int, str]:
    """Run the verifier exactly as an expert would: separate process, no DB/S3 settings in its env."""
    proc = subprocess.run(
        [sys.executable, "-m", "edisc_custody.cli", str(pkg)],
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
    code, out = _cli(package)
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
    code, out = _cli(package)
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
    code, out = _cli(package)
    assert code == 1
    assert "disagrees with the WORM anchor (chain rewritten)" in out


async def test_item_content_hash_change_breaks_merkle_and_key(package: Path) -> None:
    items = _read(package, "items.jsonl")
    items[5]["content_hash"] = "ee" * 32
    _write(package, "items.jsonl", items, fix_manifest=True)
    code, out = _cli(package)
    assert code == 1
    assert "Merkle root mismatch" in out
    assert "idempotency_key does not match" in out


async def test_evidence_bytes_altered(package: Path) -> None:
    obj = next((package / "objects").iterdir())
    obj.write_bytes(obj.read_bytes().replace(b"msg", b"MSG", 1))
    code, out = _cli(package)
    assert code == 1
    assert "bytes do not match recorded sha256" in out


async def test_raw_hash_checked_against_page_fragment(package: Path) -> None:
    items = _read(package, "items.jsonl")
    items[0]["json_path"] = (
        "$.messages[1]" if items[0]["json_path"] != "$.messages[1]" else "$.messages[0]"
    )
    _write(package, "items.jsonl", items, fix_manifest=True)
    code, out = _cli(package)
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
    code, out = _cli(package)
    assert code == 1
    assert "no matching WORM seal" in out


def test_unreadable_package_exits_2(tmp_path: Path) -> None:
    code, out = _cli(tmp_path)
    assert code == 2, out
