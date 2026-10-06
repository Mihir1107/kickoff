"""Renders and their external natives in the collection report (ADR 0018 §1, M16 step 1): every
render sealed at the snapshot, with its identity, head, seal and every native kept outside the zip,
from the custody-anchored records."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_worker.renders import RenderRun
from edisc_worker.report_loader import ReportLoader

from ..normalizer.harness import Sessions
from .conftest import drive
from .test_render_natives import _expected_natives, _rendered, _settings


async def test_renders_with_external_natives_are_listed(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path
) -> None:
    rs = _settings(settings)
    t, job_id, render_id = await _rendered(app_sessions, s3, rs)
    assert (await drive(RenderRun(app_sessions, s3, rs), t.tenant_id, render_id))[
        "status"
    ] == "completed"
    expected = await _expected_natives(app_sessions, s3, rs, t, job_id)
    files: dict[str, bytes] = {}

    async def sink(name: str, chunks: AsyncIterator[bytes]) -> None:
        files[name] = b"".join([c async for c in chunks])

    built = await ReportLoader(app_sessions, s3, rs, tenant_id=t.tenant_id, job_id=job_id).build(
        sink
    )
    (row,) = [json.loads(x) for x in files["renders.jsonl"].splitlines()]
    assert row["render_id"] == str(render_id) and row["status"] == "completed"
    assert row["seal_key"] and row["seal_version_id"] and row["head_seq"]
    assert {e["sha256"]: (e["size"], e["file_ords"]) for e in row["externals"]} == expected
    assert row["natives"] == len(expected) and row["natives_bytes"] == sum(
        s for s, _ in expected.values()
    )
    assert built.document["renders"]["renders"] == 1
