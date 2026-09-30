"""Version pinning: a newer version planted at an evidence key is never served, and is reported."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.export import export_package
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter

from ..custody.conftest import run_job

Sessions = async_sessionmaker[AsyncSession]


async def _collect(writer: EvidenceWriter, stream: object) -> bytes:
    return b"".join([c async for c in stream])  # type: ignore[attr-defined]


async def test_shadow_version_is_never_served_and_is_reported(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path
) -> None:
    writer = EvidenceWriter(app_sessions, s3, settings)
    job = await run_job(app_sessions, s3, settings, batches=3, items_per_batch=2)
    async with tenant_tx(app_sessions, job.tenant_id) as s:
        ev = (
            await s.execute(
                text(
                    "SELECT id, storage_key, sha256, version_id FROM evidence_objects WHERE job_id = :j AND kind = 'page'"
                    " ORDER BY created_at LIMIT 1"
                ),
                {"j": job.job_id},
            )
        ).one()
    original = await _collect(writer, writer.open(tenant_id=job.tenant_id, evidence_id=ev.id))
    assert hashlib.sha256(original).hexdigest() == ev.sha256

    # Someone outside our advisory lock (bug, other service, operator) writes a newer version at the key.
    forged = json.dumps({"messages": [{"ts": "1.0", "text": "forged"}]}).encode()
    planted = await s3.put_object(
        Bucket=settings.s3_evidence_bucket, Key=ev.storage_key, Body=forged
    )
    latest = await (await s3.get_object(Bucket=settings.s3_evidence_bucket, Key=ev.storage_key))[
        "Body"
    ].read()
    assert latest == forged  # "latest" is now the shadow

    # review/read path serves the pinned original
    assert (
        await _collect(writer, writer.open(tenant_id=job.tenant_id, evidence_id=ev.id)) == original
    )
    # verify checks the original and reports the shadow
    result = await writer.verify(tenant_id=job.tenant_id, evidence_id=ev.id)
    assert result.bytes_match
    assert result.shadow_versions == (planted["VersionId"],)
    assert not result.clean

    # export carries the original bytes and records the shadow
    pkg = await export_package(
        app_sessions,
        s3,
        settings,
        tenant_id=job.tenant_id,
        job_id=job.job_id,
        dest=tmp_path / "pkg",
    )
    assert (pkg / "objects" / ev.sha256).read_bytes() == original
    rec = next(
        json.loads(line)
        for line in (pkg / "evidence.jsonl").read_text().splitlines()
        if json.loads(line)["id"] == str(ev.id)
    )
    assert rec["version_id"] == ev.version_id
    assert rec["shadow_versions"] == [planted["VersionId"]]

    # edisc-verify: everything else verifies (items re-hash against the ORIGINAL page), shadow reported
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "edisc_custody.cli", str(pkg)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    errors = [line for line in proc.stdout.splitlines() if line.strip().startswith("ERROR")]
    assert len(errors) == 1, proc.stdout
    assert "storage incident" in errors[0]
    assert planted["VersionId"] in errors[0]
