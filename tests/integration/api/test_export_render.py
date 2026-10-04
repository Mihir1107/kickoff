"""M15 step 3 on an export job (ADR 0014 + 0015): pages are entries of the LOCKED export, read and
verified from the archive's pinned version, conversation types come from the export's own metadata,
and the completeness basis is "archive" with the ADR 0014 caveat in every file."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportOptions, write_export
from edisc_core.ids import new_id
from edisc_core.schemas import ARCHIVE_CAVEAT
from edisc_db.session import tenant_tx
from edisc_renderers.rsmf import RenderOptions
from edisc_worker.render_store import render_and_store

from ...unit.renderers.emlcheck import check_eml, custom
from ..renders.test_render_store import _read
from .conftest import Api, FileHost, TenantCtx, collection_workers
from .test_export_collection import SPEC, _export_connection, _job, _scope
from .test_jobs import make_world


async def test_an_export_job_renders_from_archive_entries(api: Api, tenant: TenantCtx) -> None:
    ds = Dataset(SPEC)
    buf = io.BytesIO()
    write_export(ds, buf, ExportOptions())
    async with collection_workers(api, FileHost(ds)) as exp:
        w = await make_world(exp, tenant, SPEC)
        start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, buf.getvalue())
            job = await _job(
                c,
                w.matter,
                {"connection_id": export["connection_id"], "scopes": [_scope(start, 3)]},
            )
        job_id = uuid.UUID(job["id"])
        out = await render_and_store(
            exp.sessions, exp.s3, exp.settings, tenant_id=tenant.tenant_id, job_id=job_id,
            render_id=new_id(), options=RenderOptions(time_zone="America/New_York"),
        )  # fmt: skip

    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        entries = (
            await s.execute(
                text(
                    "SELECT count(*) FROM evidence_objects WHERE job_id = :j AND kind = 'archive_entry'"
                ),
                {"j": job_id},
            )
        ).scalar_one()
    assert job["status"] == "completed_against_archive"
    assert entries > 0 and out.verified_objects >= 1  # entries read back from the locked zip
    assert out.reconciliation.items_in == out.reconciliation.events_out > 0
    kinds = {c.id: c.kind for c in ds.conversations()}
    for f in out.files:
        parsed = check_eml(await _read(exp.s3, exp.settings, f.storage_key, f.version_id))
        assert parsed.headers["X-RSMF-CompletenessBasis"] == "archive"
        assert ARCHIVE_CAVEAT in parsed.text
        (conv,) = parsed.manifest["conversations"]
        slack_type = {c["name"]: c["value"] for c in conv["custom"]}["slack.conversation_type"]
        expected = {
            "channel": "public_channel",
            "private_channel": "private_channel",
            "dm": "im",
            "group_dm": "mpim",
        }[kinds[conv["id"]]]
        assert slack_type == expected
        assert conv["type"] == ("direct" if expected in ("im", "mpim") else "channel")
        assert all("edisc.source_item_id" in custom(e) for e in parsed.manifest["events"])
