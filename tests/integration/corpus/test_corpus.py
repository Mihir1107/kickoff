"""The synthetic render corpus, live dialects (M15 step 5): collect through the real pipeline, render
through the render steps, check against the oracle, the structural EML checks, a golden and
``edisc-verify``. Export cases: `tests/integration/api/test_corpus_export.py`."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from types_aiobotocore_s3 import S3Client

import edisc_renderers.rsmf.render as render_module
from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connectors_base.types import CollectionScope, Connection
from edisc_core.ids import new_id
from edisc_core.schemas import ScopeType
from edisc_core.settings import Settings
from edisc_worker.pipeline import CrashHooks, Pipeline
from edisc_worker.renders import RenderRun

from ...unit.dummy.conftest import RecordingLimiter
from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..renders.conftest import drive, new_render, render_state
from .cases import CASES, LIVE, Case, real_exports
from .check import check_against_oracle, check_golden, check_package, stored, stored_natives
from .oracle import COVERAGE, build


async def collect(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, case: Case
) -> uuid.UUID:
    """Every epoch of the case in order (earlier ones over the whole dataset); returns the last job."""
    ds = Dataset(case.spec)
    job_id = None
    for n, epoch in enumerate(case.epochs):
        last = n == len(case.epochs) - 1
        first = case.first_day if last else 0
        start = datetime.combine(ds.day(first), datetime.min.time(), tzinfo=UTC)
        end = datetime.combine(ds.day(ds.n_days(epoch)), datetime.min.time(), tzinfo=UTC)
        conn = Connection(
            t.tenant_id, t.connection_id, "dummy", case.spec.workspace_id,
            {"spec": case.spec.model_dump(mode="json"), "epoch": epoch},
        )  # fmt: skip
        if case.custodians:
            members = ds.conversations()[0].members[: case.custodians]
            scopes = [
                CollectionScope(ScopeType.CUSTODIAN, u, start, end, case.policy) for u in members
            ]
        else:
            scopes = [
                scope_for_days(
                    "*", start, ds.n_days(epoch) - first, thread_parent_policy=case.policy
                )
            ]
        job_id = new_id()
        p = Pipeline(sessions, s3, settings, DummyConnector(RecordingLimiter()), CrashHooks())
        await p.start_job(
            tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
            connection_id=t.connection_id, scopes=scopes, requested_by="corpus",
        )  # fmt: skip
        await p.run(tenant_id=t.tenant_id, job_id=job_id, conn=conn)
    assert job_id is not None
    return job_id


@pytest.mark.parametrize("name", sorted(LIVE))
async def test_live_case(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path, name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    case = LIVE[name]
    if case.entry_limit is not None:  # read by the renderer at call time; renders run in-process
        monkeypatch.setattr(render_module, "MAX_PART_ENTRIES", case.entry_limit)
    rs = settings.model_copy(update={"render_files_batch_size": case.batch_size})
    oracle = build(case)
    t = await new_tenant(app_sessions)
    job_id = await collect(app_sessions, s3, rs, t, case)
    render_id = await new_render(app_sessions, t.tenant_id, job_id, case.options)
    result = await drive(RenderRun(app_sessions, s3, rs), t.tenant_id, render_id)
    assert result["status"] == "completed", result
    st = await render_state(app_sessions, t.tenant_id, render_id)
    files = await stored(app_sessions, s3, rs, t.tenant_id, render_id)
    manifests = check_against_oracle(case, oracle, files, dict(st["row"].summary))
    if case.expect_files is not None:  # the batch boundary: ceil(files / B) batch events
        batches = st["types"].count("render_files_batch")
        assert batches == -(-case.expect_files // case.batch_size)
    if case.entry_limit is not None:
        assert all(r["attachment_count"] <= case.entry_limit - 1 for r, _ in files)
        assert any(r["parts"] > 1 for r, _ in files), "the entry limit split a slice"
    # natives: one per SHA-256, its bytes the collected file's, every reference accounted for
    natives = await stored_natives(app_sessions, s3, rs, t.tenant_id, render_id)
    referenced = {
        v.split("sha256:", 1)[1]
        for m in manifests.values()
        for e in m["events"]
        for p in e.get("custom", [])
        if p["name"] == "edisc.file_external"
        for v in [p["value"]]
    }
    assert set(natives) == referenced
    assert all(hashlib.sha256(data).hexdigest() == sha for sha, data in natives.items())
    check_golden(name, manifests)
    await check_package(app_sessions, s3, rs, t.tenant_id, render_id, tmp_path / "pkg")


def test_the_corpus_covers_the_matrix() -> None:
    """Every coverage item is exercised by at least one case (computed from the oracle; each case's
    test then proves its render contains it), so the corpus can never silently lose coverage."""
    seen: set[str] = set()
    for case in CASES.values():
        seen |= build(case).features
    assert COVERAGE - seen == set(), sorted(COVERAGE - seen)


def test_real_exports_are_listed() -> None:
    """Real exports join when provided; until then the list is empty, visibly (no silent skip)."""
    for path in real_exports():
        assert (path / "expected.json").is_file(), (
            f"{path.name}: needs a hand-reviewed expected.json"
        )
    print(f"real exports in the corpus: {[p.name for p in real_exports()] or 'none yet'}")
