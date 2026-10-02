"""edisc-verify package format /2 (ADR 0014 section 2, M14.6): a custody package of an EXPORT job
carries its zip either embedded or referenced by SHA-256 (``--archive``). The archive's hash is checked
before any entry is opened; entries are then found by exact name, CRC-checked, decompressed and hashed,
and every item fragment is checked against them. Runs the verifier as an expert would: a separate
process with no database or cloud settings."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from edisc_custody.export import export_package

from .conftest import Api, FileHost, TenantCtx, collection_workers
from .test_export_collection import THREADS, _export_connection, _job, _scope, _zip
from .test_jobs import make_world


def verify(pkg: Path, *archives: Path) -> tuple[int, str, dict[str, object]]:
    args = [sys.executable, "-m", "edisc_custody.cli", str(pkg), "--json"]
    for a in archives:
        args += ["--archive", str(a)]
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=120,
        check=False,
    )
    report = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
    return proc.returncode, proc.stdout + proc.stderr, report


@pytest.fixture
async def export_job(api: Api, tenant: TenantCtx, tmp_path: Path) -> tuple[Api, uuid.UUID, bytes]:
    """An export job whose items live in three day files (thread context across files)."""
    data = _zip(THREADS)
    async with collection_workers(api, FileHost()) as exp:
        w = await make_world(exp, tenant)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, data)
            job = await _job(
                c,
                w.matter,
                {
                    "connection_id": export["connection_id"],
                    "scopes": [_scope(datetime(2026, 1, 5, tzinfo=UTC), 1)],
                },
            )
    assert job["status"] == "completed_against_archive"
    return exp, uuid.UUID(job["id"]), data


async def _package(exp: Api, tenant: TenantCtx, job_id: uuid.UUID, dest: Path, mode: str) -> Path:
    return await export_package(
        exp.sessions, exp.s3, exp.settings, tenant_id=tenant.tenant_id, job_id=job_id,
        dest=dest, archives=mode,  # type: ignore[arg-type]
    )  # fmt: skip


async def test_embedded_archive_verifies_offline(
    export_job: tuple[Api, uuid.UUID, bytes], tenant: TenantCtx, tmp_path: Path
) -> None:
    exp, job_id, data = export_job
    pkg = await _package(exp, tenant, job_id, tmp_path / "pkg", "embed")
    manifest = json.loads((pkg / "manifest.json").read_bytes())
    assert manifest["format"] == "edisc-custody-package/2"
    (archive,) = manifest["archives"]
    assert archive["embedded"] and archive["sha256"] == hashlib.sha256(data).hexdigest()
    assert (pkg / "objects" / archive["sha256"]).read_bytes() == data
    code, out, report = verify(pkg)
    assert code == 0, out
    # users.json, the 5 Jan day file, and the 1 and 9 Jan files read for thread context
    assert (report["archives_checked"], report["entries_checked"]) == (1, 4)
    assert report["items_checked"] > 0 and "archive entries extracted and verified" in verify_text(
        pkg
    )

    # a single flipped byte in the embedded zip: refused at the archive hash, no entry opened
    zip_path = pkg / "objects" / archive["sha256"]
    tampered = bytearray(zip_path.read_bytes())
    tampered[len(tampered) // 2] ^= 0x01
    zip_path.write_bytes(bytes(tampered))
    code, out, report = verify(pkg)
    assert code == 1 and "SHA-256/size do not match the record" in out
    assert report["entries_checked"] == 0


def verify_text(pkg: Path, *archives: Path) -> str:
    args = [sys.executable, "-m", "edisc_custody.cli", str(pkg)]
    for a in archives:
        args += ["--archive", str(a)]
    proc = subprocess.run(args, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
                          timeout=120, check=False)  # fmt: skip
    return proc.stdout


async def test_referenced_archive_is_supplied_and_hash_checked_first(
    export_job: tuple[Api, uuid.UUID, bytes], tenant: TenantCtx, tmp_path: Path
) -> None:
    exp, job_id, data = export_job
    pkg = await _package(exp, tenant, job_id, tmp_path / "pkg", "reference")
    manifest = json.loads((pkg / "manifest.json").read_bytes())
    (archive,) = manifest["archives"]
    assert not archive["embedded"] and not (pkg / "objects" / archive["sha256"]).exists()
    assert archive["limits"]["max_entries"] > 0  # verified under the export's own limits

    code, out, report = verify(pkg)  # nothing supplied
    assert code == 1 and "supply the archive with --archive" in out
    assert report["entries_checked"] == 0

    supplied = tmp_path / "any-name.zip"  # matched by content, not by file name
    supplied.write_bytes(data)
    code, out, report = verify(pkg, supplied)
    assert code == 0, out
    assert (report["archives_checked"], report["entries_checked"]) == (1, 4)

    wrong = tmp_path / "other.zip"
    wrong.write_bytes(_zip({"general/2026-01-05.json": []}))
    code, out, report = verify(pkg, wrong)
    assert code == 1 and report["entries_checked"] == 0


async def test_entry_records_are_checked_against_the_archive(
    export_job: tuple[Api, uuid.UUID, bytes], tenant: TenantCtx, tmp_path: Path
) -> None:
    """A package whose evidence records were rewritten consistently (manifest included) still fails:
    the entry's recorded CRC-32 or decompressed SHA-256 no longer matches what the archive holds."""
    exp, job_id, _ = export_job
    pkg = await _package(exp, tenant, job_id, tmp_path / "pkg", "embed")
    for field, value in (("entry_crc32", 1), ("sha256", "00" * 32)):
        copy = tmp_path / f"pkg-{field}"
        shutil.copytree(pkg, copy)
        rows = [json.loads(line) for line in (copy / "evidence.jsonl").read_text().splitlines()]
        target = next(
            r for r in rows if r["kind"] == "archive_entry" and "2026-01-05" in r["entry_path"]
        )
        target[field] = value
        if field == "sha256":
            target["source_sha256"] = value
        from edisc_core.canonical import canonical_json

        body = b"".join(canonical_json(r) + b"\n" for r in rows)
        (copy / "evidence.jsonl").write_bytes(body)
        manifest = json.loads((copy / "manifest.json").read_bytes())
        manifest["files"]["evidence.jsonl"] = {
            "sha256": hashlib.sha256(body).hexdigest(), "lines": len(rows),
        }  # fmt: skip
        (copy / "manifest.json").write_bytes(canonical_json(manifest))
        code, out, report = verify(copy)
        assert code == 1, (field, out)
        assert report["entries_checked"] == 3  # the other entries still verify
