"""A lone surrogate quoted by a recorded error (ADR 0018 §23): the unit fails through the pipeline,
the chain and the database hold valid text, the chain also the exact code units (`error_utf16`),
and the report states the exact error: `units.jsonl` and `report.json` valid with `lone_surrogates`,
counted, the HTML showing the marker, the report completed and verified."""

from __future__ import annotations

import json

from sqlalchemy import text
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.reports import ReportRun

from ..normalizer.harness import Sessions, new_tenant
from ..reports.conftest import assert_completed, drive, new_report, report_state, stored_bytes
from .synthetic import sealed_job, unit_keys

EXACT = "channel name " + chr(0xD83D) + " not resolvable"
UNITS = [ord(c) for c in EXACT]


async def test_an_error_quoting_a_lone_surrogate_is_recorded_and_reported_exactly(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t = await new_tenant(app_sessions)
    keys = unit_keys(3)
    job = await sealed_job(app_sessions, s3, settings, t, keys, failed={keys[1]: EXACT})
    async with tenant_tx(app_sessions, t.tenant_id) as s:
        payload = (await s.execute(text(
            "SELECT payload FROM custody_events WHERE stream_id = :j AND event_type = 'unit_failed'"
        ), {"j": job})).scalar_one()  # fmt: skip
        last_error = (await s.execute(text(
            "SELECT last_error FROM work_units WHERE job_id = :j AND unit_key = :k"
        ), {"j": job, "k": keys[1]})).scalar_one()  # fmt: skip
    assert payload["error"] == "channel name � not resolvable"
    assert payload["error_utf16"] == UNITS
    assert last_error == "SourceError: channel name � not resolvable"

    report_id = await new_report(app_sessions, t, job)
    out = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert out["status"] == "completed", out
    await assert_completed(app_sessions, s3, settings, t, job, report_id)  # chain + oracle bytes
    st = await report_state(app_sessions, t.tenant_id, report_id)
    files = await stored_bytes(s3, settings, t, report_id, st["files"])
    doc = json.loads(files["report.json"])
    assert doc["divergences"]["divergences"] == []  # valid text on both sides: they agree
    assert doc["integrity"]["lone_surrogates"]["count"] >= 2  # units.jsonl + report.json rows
    (row,) = [json.loads(x) for x in files["units.jsonl"].splitlines()
              if json.loads(x)["unit_key"] == keys[1]]  # fmt: skip
    assert row["error"] == "channel name � not resolvable"
    assert row["lone_surrogates"] == {"/error": UNITS}
    units_rec = next(f for f in doc["integrity"]["files"] if f["name"] == "units.jsonl")
    assert units_rec["lone_surrogates"] == 1
    page = files["report.html"].decode("utf-8")
    assert 'channel name <span class="cp">U+D83D</span> not resolvable' in page
