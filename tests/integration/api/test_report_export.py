"""The collection report of an export job and of a live job through the real API (ADR 0018 §4, §7.2,
§15): archive-relative completeness is never clean and quotes the caveat byte for byte; the access
facts (plan tier, granted scopes, blind spots, the export's id and SHA-256) come from the chain."""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportOptions, write_export
from edisc_core.schemas import ARCHIVE_CAVEAT
from edisc_worker.report_loader import ReportLoader

from .conftest import Api, FileHost, TenantCtx, collection_workers
from .test_export_collection import SPEC, _export_connection, _job, _scope
from .test_jobs import make_world


async def _report(
    api: Api, t: TenantCtx, job_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    files: dict[str, bytes] = {}

    async def sink(name: str, chunks: AsyncIterator[bytes]) -> None:
        files[name] = b"".join([c async for c in chunks])

    built = await ReportLoader(
        api.sessions, api.s3, api.settings, tenant_id=t.tenant_id, job_id=uuid.UUID(job_id)
    ).build(sink)
    return built.document, [json.loads(x) for x in files["units.jsonl"].splitlines()]


async def test_an_export_job_reports_archive_relative_completeness_with_the_caveat(
    api: Api, tenant: TenantCtx
) -> None:
    ds = Dataset(SPEC)
    buf = io.BytesIO()
    write_export(ds, buf, ExportOptions())
    async with collection_workers(api, FileHost(ds)) as exp:
        w = await make_world(exp, tenant, SPEC)
        start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, buf.getvalue())
            live = await _job(
                c, w.matter, {"connection_id": w.connection, "scopes": [_scope(start, 3)]}
            )
            archive = await _job(
                c,
                w.matter,
                {"connection_id": export["connection_id"], "scopes": [_scope(start, 3)]},
            )
        doc, units = await _report(exp, tenant, archive["id"])
        live_doc, _ = await _report(exp, tenant, live["id"])

    assert doc["job"]["status"] == "completed_against_archive"
    assert doc["job"]["clean"] is False and doc["job"]["clean_basis"] == "archive"
    assert doc["job"]["archive_caveat"] == ARCHIVE_CAVEAT
    assert doc["banner"] == ["COMPLETE RELATIVE TO THE PROVIDED EXPORT ONLY", ARCHIVE_CAVEAT]
    counted = [u for u in units if u["kind"] != "directory"]
    assert counted and {u["recon_status"] for u in counted} == {"matched_against_archive"}
    assert all(u["basis"] == "archive" for u in counted)
    days = {(c.id, ds.day(d).isoformat()) for c in ds.conversations() for d in range(3)}
    assert days <= {(u["conversation_id"], u["day"]) for u in counted}
    access = doc["access"]
    assert access["source"] == "job_chain"
    assert access["export"] == {"id": export["id"], "sha256": export["sha256"]}
    assert access["blind_spots"] == export["findings"]["blind_spots"]
    assert access["plan_tier"] == export["detected_tier"]

    # the live job's connection was created through the API: its facts are recorded too
    assert live_doc["job"]["clean"] is True
    live_access = live_doc["access"]
    assert live_access["plan_tier"] != "UNKNOWN (not recorded)"
    assert isinstance(live_access["granted_scopes"], list) and live_access["granted_scopes"]
    assert isinstance(live_access["blind_spots"], list)
    assert live_access["export"] is None
