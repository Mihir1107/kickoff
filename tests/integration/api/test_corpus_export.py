"""The synthetic render corpus, export dialect (M15 step 5): upload, lock and validate the synthetic
export through the API, collect from it, render, then the same checks as the live cases."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportOptions, write_export
from edisc_worker.renders import RenderRun

from ..corpus.cases import EXPORT
from ..corpus.check import check_against_oracle, check_golden, check_package, stored
from ..corpus.oracle import build
from ..renders.conftest import drive, new_render, render_state
from .conftest import Api, FileHost, TenantCtx, collection_workers
from .test_export_collection import _export_connection, _job, _scope
from .test_jobs import make_world


@pytest.mark.parametrize("name", sorted(EXPORT))
async def test_export_case(api: Api, tenant: TenantCtx, tmp_path: Path, name: str) -> None:
    case = EXPORT[name]
    ds = Dataset(case.spec)
    buf = io.BytesIO()
    write_export(ds, buf, ExportOptions(tier=case.export_tier))
    oracle = build(case)
    async with collection_workers(api, FileHost(ds)) as exp:
        w = await make_world(exp, tenant, case.spec)
        start = datetime.combine(ds.day(case.first_day), datetime.min.time(), tzinfo=UTC)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, buf.getvalue())
            job = await _job(
                c, w.matter,
                {"connection_id": export["connection_id"],
                 "scopes": [_scope(start, ds.n_days(0) - case.first_day, case.policy.value)]},
            )  # fmt: skip
        rs = exp.settings.model_copy(update={"render_files_batch_size": case.batch_size})
        job_id = uuid.UUID(job["id"])
        render_id = await new_render(exp.sessions, tenant.tenant_id, job_id, case.options)
        result = await drive(RenderRun(exp.sessions, exp.s3, rs), tenant.tenant_id, render_id)
        assert result["status"] == "completed", result
        st = await render_state(exp.sessions, tenant.tenant_id, render_id)
        files = await stored(exp.sessions, exp.s3, rs, tenant.tenant_id, render_id)
        manifests = check_against_oracle(case, oracle, files, dict(st["row"].summary))
        check_golden(name, manifests)
        await check_package(exp.sessions, exp.s3, rs, tenant.tenant_id, render_id, tmp_path / "pkg")
