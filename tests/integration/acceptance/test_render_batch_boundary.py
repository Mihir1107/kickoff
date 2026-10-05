"""The render file batch boundary at the PRODUCTION batch size (500): a render of exactly 500 files
commits one ``render_files_batch``, one of 501 files commits two (500 + 1). Each file is one
conversation-day, so the jobs have 500 and 501 units. The same oracle, golden and ``edisc-verify``
checks as the corpus (`tests/integration/corpus`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_worker.renders import RenderRun

from ..corpus.cases import PRODUCTION_BATCH, Case, _spec
from ..corpus.check import check_against_oracle, check_golden, check_package, stored
from ..corpus.oracle import build
from ..corpus.test_corpus import collect
from ..normalizer.harness import Sessions, new_tenant
from ..renders.conftest import drive, new_render, render_state


@pytest.mark.timeout(900)
@pytest.mark.parametrize("files", [PRODUCTION_BATCH, PRODUCTION_BATCH + 1])
async def test_the_production_batch_boundary(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path, files: int
) -> None:
    assert settings.render_files_batch_size == PRODUCTION_BATCH  # the default, not a test setting
    case = Case(
        _spec(seed=44, conversations=1, days=files, messages_per_unit=12, page_size=12, p_file=0.0),
        batch_size=PRODUCTION_BATCH, expect_files=files,
    )  # fmt: skip
    t = await new_tenant(app_sessions)
    job_id = await collect(app_sessions, s3, settings, t, case)
    render_id = await new_render(app_sessions, t.tenant_id, job_id)
    result = await drive(RenderRun(app_sessions, s3, settings), t.tenant_id, render_id)
    assert result["status"] == "completed"
    st = await render_state(app_sessions, t.tenant_id, render_id)
    batches = [e.payload for e in st["events"] if e.event_type == "render_files_batch"]
    assert [b["file_count"] for b in batches] == (
        [PRODUCTION_BATCH] if files == PRODUCTION_BATCH else [PRODUCTION_BATCH, 1]
    )
    out = await stored(app_sessions, s3, settings, t.tenant_id, render_id)
    manifests = check_against_oracle(case, build(case), out, dict(st["row"].summary))
    check_golden(f"production_batch_{files}", manifests)
    await check_package(app_sessions, s3, settings, t.tenant_id, render_id, tmp_path / "pkg")
