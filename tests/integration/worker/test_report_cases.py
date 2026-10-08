"""Collection report cases that need the real workflows (ADR 0018 §15): failed units, a cancelled
job, a failed job, a pause with re-authorization by a named principal. Expected values from the
dummy `Dataset` and the injected conditions (`tests/integration/report/oracle.py`)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from edisc_connector_dummy.dataset import Dataset
from edisc_core.schemas import JobStatus
from edisc_db.session import tenant_tx
from edisc_worker.pipeline import Pipeline
from edisc_worker.report_loader import BuiltReport, ReportLoader

from ..report import oracle
from .conftest import Harness, fast, queue, replace_cfg, spec
from .test_workflows import AnchorConflictAt

pytestmark = pytest.mark.timeout(120)


async def _report(
    h: Harness, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> tuple[BuiltReport, dict[str, list[dict[str, Any]]]]:
    files: dict[str, bytes] = {}

    async def sink(name: str, chunks: AsyncIterator[bytes]) -> None:
        files[name] = b"".join([c async for c in chunks])

    built = await ReportLoader(
        h.sessions, h.s3, h.settings, tenant_id=tenant_id, job_id=job_id
    ).build(sink)
    return built, {
        k: [json.loads(x) for x in v.splitlines()]
        for k, v in files.items()
        if not k.endswith(".html")  # report.html is not JSON; these cases inspect the JSON/JSONL
    }


async def test_failed_units_are_exceptions_with_their_error(harness: Harness) -> None:
    sp = spec(failures={"corrupt_conversations": [1]})
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    async with harness.worker(q := queue(), settings=fast(harness.settings)):
        status = await (await harness.start(t, job_id, q)).result()
    want = oracle.units(Dataset(sp), 0)
    assert status == oracle.job_status(want) == JobStatus.COMPLETED_WITH_FAILED_UNITS.value
    built, files = await _report(harness, t.tenant_id, job_id)
    oracle.check(built.document, files["units.jsonl"], want, status)
    failed = [r for r in files["units.jsonl"] if r["status"] == "failed"]
    assert failed and all(r["error_type"] == "InvalidCursorError" and r["error"] for r in failed)
    shown = built.document["exceptions"]["units"]["rows"]
    assert [r["unit_key"] for r in shown[: len(failed)]] == sorted(r["unit_key"] for r in failed)
    assert built.document["banner"][0].startswith(
        "NOT COMPLETE: status completed_with_failed_units"
    )


async def test_a_cancelled_job_reports_its_unsettled_units_from_the_database(
    harness: Harness,
) -> None:
    sp = spec(conversations=4, messages_per_unit=30)
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    async with harness.worker(q := queue(), settings=fast(harness.settings)):
        handle = await harness.start(t, job_id, q, replace_cfg(max_units_in_flight=1))
        for _ in range(200):
            async with tenant_tx(harness.sessions, t.tenant_id) as s:
                batches = (
                    await s.execute(
                        text("SELECT count(*) FROM custody_events WHERE stream_id = :j"
                             " AND event_type = 'items_collected'"),
                        {"j": job_id},
                    )
                ).scalar_one()  # fmt: skip
            if batches >= 2:
                break
            await asyncio.sleep(0.05)
        await handle.signal("cancel")
        status = await handle.result()
    assert status == JobStatus.CANCELLED.value
    built, files = await _report(harness, t.tenant_id, job_id)
    units = files["units.jsonl"]
    assert {r["unit_key"] for r in units} == set(oracle.units(Dataset(sp), 0))
    unsettled = [r for r in units if r["source"] == "database"]
    assert unsettled and all(r["status"] not in ("done", "failed") for r in unsettled)
    assert all(r["source"] == "job_chain" for r in units if r["status"] in ("done", "failed"))
    assert built.document["job"]["status"] == "cancelled" and not built.clean
    assert built.document["divergences"]["divergences"] == []
    assert built.document["banner"][0].startswith("NOT COMPLETE: status cancelled")
    assert "cancel_requested" in {a["event_type"] for a in built.document["actors"]["custody"]}


async def test_a_failed_job_still_gets_a_report_that_says_so(harness: Harness) -> None:
    sp = spec(conversations=4, messages_per_unit=24)
    t = await harness.tenant(sp)
    job_id = await harness.create_job(t, sp)
    async with harness.worker(
        q := queue(), hooks=AnchorConflictAt(3), settings=fast(harness.settings)
    ):
        status = await (await harness.start(t, job_id, q)).result()
    assert status == JobStatus.FAILED.value
    built, _ = await _report(harness, t.tenant_id, job_id)
    assert built.document["job"]["status"] == "failed" and not built.clean
    assert built.document["custody_verification"]["ok"] is True
    assert built.document["banner"][0].startswith("NOT COMPLETE: status failed")


async def test_a_pause_records_who_reauthorized_and_for_how_long(harness: Harness) -> None:
    sp = spec()
    t = await harness.tenant(sp, auth_revoked=True)
    job_id = await harness.create_job(t, sp)
    async with harness.worker(q := queue(), settings=fast(harness.settings)):
        handle = await harness.start(t, job_id, q)
        for _ in range(100):
            if (await harness.job(t, job_id)).status == "paused_awaiting_reauth":
                break
            await asyncio.sleep(0.2)
        await harness.set_config(t, sp)
        await Pipeline(
            harness.sessions, harness.s3, harness.settings, harness.activities().connectors["dummy"]
        ).resume_connection(tenant_id=t.tenant_id, connection_id=t.connection_id, actor="user:dana")
        await handle.signal("wake")
        status = await handle.result()
    assert status == JobStatus.COMPLETED.value
    built, files = await _report(harness, t.tenant_id, job_id)
    (pause,) = built.document["pauses"]["pauses"]
    assert pause["resumed_by"] == "user:dana" and pause["reason"]
    assert pause["duration_ms"] > 0 and built.document["pauses"]["never_resumed"] == 0
    oracle.check(built.document, files["units.jsonl"], oracle.units(Dataset(sp), 0), status)
    assert built.clean  # a pause that was resolved leaves a clean job clean
