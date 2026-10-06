"""Phase 1 resume acceptance (ADR 0012 section 8): 50,000 messages through Temporal with 3 worker
PROCESSES, random SIGKILLs and one kill-all/restart. The result must equal the dataset oracle exactly.

The same driver (scripts/resume_soak.py) runs the documented 1M manual run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.schemas import JobStatus
from edisc_core.settings import Settings

from ..normalizer.harness import Tenant
from ..pipeline.conftest import JobRun, assert_invariants

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "resume_soak.py"


def _soak() -> ModuleType:
    spec = importlib.util.spec_from_file_location("resume_soak", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.timeout(
    1500
)  # explicit exception to the 120 s default: this is the 50k acceptance run
async def test_50k_messages_survive_sigkills_and_a_full_restart_exactly(
    app_sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    temporal: Client,
    limiter: RateLimiter,
    tmp_path: Path,
) -> None:
    soak = _soak()
    cfg = soak.SoakConfig(workdir=tmp_path / "workers")
    assert cfg.messages == 50_000
    result = await soak.soak(
        cfg, sessions=app_sessions, s3=s3, settings=settings, temporal=temporal, limiter=limiter
    )
    print(f"50k soak: {result.seconds:.0f}s, kills: {result.kills}")
    assert len(result.kills) == cfg.kills + 1, (
        f"job finished before every kill fired: {result.kills}"
    )
    assert any(k.startswith("ALL") for k in result.kills)
    assert result.problems == []
    # oracle-exact derived state, zero duplicates, no pending evidence, valid sealed chain
    tenant = Tenant(result.tenant_id, None, None, None)  # type: ignore[arg-type]
    await assert_invariants(
        app_sessions,
        s3,
        settings,
        tenant,
        cfg.spec(),
        JobRun(result.job_id, JobStatus(result.status), True),
        0,
    )
    # the job's collection report states the oracle too (ADR 0018 §12)
    import json
    from collections.abc import AsyncIterator

    from edisc_connector_dummy.dataset import Dataset
    from edisc_worker.report_loader import ReportLoader

    from ..report import oracle

    units: list[dict[str, object]] = []

    async def sink(name: str, chunks: AsyncIterator[bytes]) -> None:
        async for chunk in chunks:
            if name == "units.jsonl":
                units.extend(json.loads(line) for line in chunk.splitlines())

    built = await ReportLoader(
        app_sessions, s3, settings, tenant_id=result.tenant_id, job_id=result.job_id
    ).build(sink)
    want = oracle.units(Dataset(cfg.spec()), 0)
    oracle.check(built.document, units, want, oracle.job_status(want))  # type: ignore[arg-type]
    assert built.document["divergences"]["divergences"] == []
