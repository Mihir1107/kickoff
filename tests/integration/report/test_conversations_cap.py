"""`conversations.jsonl` and the per-conversation cap (ADR 0018 §1, §4.6; fix 3 before step 3).

At exactly 1,000 conversations the report lists every one and names no file; at 1,001 it lists the
1,000 worst and names the remainder by `conversations.jsonl` and its SHA-256, so the file MUST exist,
hold every conversation (in conversation-id order) and be stored and sealed with the other files."""

from __future__ import annotations

import hashlib
import json

import pytest
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_renderers.report import model as m
from edisc_worker.reports import ReportRun

from ..normalizer.harness import Sessions, new_tenant
from ..reports.conftest import drive, new_report, report_state, stored_bytes
from .synthetic import recon_of, sealed_job, unit_keys
from .test_report_model import _assert_digests, build

BASE_FILES = ["units.jsonl", "observations.jsonl", "renders.jsonl", "report.json", "report.html"]


def _expected_rows(conversations: int, days: int) -> list[dict[str, object]]:
    """From the parameters only (never from the report)."""
    out = []
    for c in range(conversations):
        recon = recon_of(f"C{c:06d}/")
        out.append({"conversation_id": f"C{c:06d}", "units": days, "worst_status": recon,
                    "units_by_status": {recon: days}, "expected": 5 * days,
                    "collected": (4 if recon == "gap" else 5) * days, "file_gaps": 0})  # fmt: skip
    return out


@pytest.mark.parametrize("conversations", [m.CAP, m.CAP + 1])
async def test_the_cap_boundary_and_the_remainder_file(
    app_sessions: Sessions, s3: S3Client, settings: Settings, conversations: int
) -> None:
    t = await new_tenant(app_sessions)
    days = 2  # two units per conversation: the fold spans units (and page boundaries)
    job = await sealed_job(app_sessions, s3, settings, t, unit_keys(conversations, days))
    built, files = await build(app_sessions, s3, settings, t, job)
    _assert_digests(built, files)
    doc = built.document
    section = doc["conversations"]
    expected = _expected_rows(conversations, days)
    worst_first = sorted(expected, key=lambda r: (r["worst_status"] != "gap", r["conversation_id"]))
    assert section["total"] == conversations
    assert section["rows"] == worst_first[: m.CAP]  # the 1,000 worst, worst status first
    html = files.data["report.html"].decode()
    assert f"{conversations:,} total; showing the 1,000 worst" in html

    if conversations <= m.CAP:
        assert sorted(files.data) == sorted(BASE_FILES)  # no file, none named
        assert section["more"] == 0 and section["more_in"] is None
        assert "conversations.jsonl" not in html
        return
    body = files.data["conversations.jsonl"]
    assert files.rows("conversations.jsonl") == expected  # every conversation, in id order
    sha = hashlib.sha256(body).hexdigest()
    assert section["more"] == 1
    assert section["more_in"] == {"name": "conversations.jsonl", "sha256": sha}
    assert f"1 more in conversations.jsonl, SHA-256 {sha}" in html
    listed = {f["name"]: f for f in doc["integrity"]["files"]}
    assert listed["conversations.jsonl"]["rows"] == conversations
    assert listed["conversations.jsonl"]["sha256"] == sha


async def test_conversations_jsonl_is_stored_and_sealed_above_the_cap(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t = await new_tenant(app_sessions)
    job = await sealed_job(app_sessions, s3, settings, t, unit_keys(m.CAP + 1))
    report_id = await new_report(app_sessions, t, job)
    final = await drive(ReportRun(app_sessions, s3, settings), t.tenant_id, report_id)
    assert final["status"] == "completed", final
    st = await report_state(app_sessions, t.tenant_id, report_id)
    names = [f.name for f in st["files"]]
    assert names == ["units.jsonl", "observations.jsonl", "renders.jsonl", "conversations.jsonl",
                     "report.json", "report.html"]  # fmt: skip
    stored = await stored_bytes(s3, settings, t, report_id, st["files"])
    named = json.loads(stored["report.json"])["conversations"]["more_in"]
    assert named == {"name": "conversations.jsonl",
                     "sha256": hashlib.sha256(stored["conversations.jsonl"]).hexdigest()}  # fmt: skip
    generated = st["events"][-1].payload
    assert "conversations.jsonl" in [f["name"] for f in generated["files"]]
