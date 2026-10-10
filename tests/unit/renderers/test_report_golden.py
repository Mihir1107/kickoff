"""Report goldens: `report.json` and `report.html` bytes per renderer version (ADR 0018 §3, §15; fix 4
before step 3). A regression guard, like the RSMF goldens: correctness comes from the oracle and the
sanitiser tests; a golden only says "the bytes did not change". They are also the INPUT of the PDF
goldens (step 3): a PDF golden is the PDF of a recorded `report.html` here, so a PDF golden can only
be recorded once the HTML it renders is pinned.

`tests/golden/report/<key>/<case>/` holds `report.json`, `report.html` and `index.json`. The key is
`<REPORT_RENDERER_VERSION>_unicode-<Unicode version>` (every input to byte identity besides the
data; the inputs below are fixed literals, not generated). Changing the output bytes without bumping
`REPORT_RENDERER_VERSION` fails here. Record a new generation (old ones stay as history):

    EDISC_RECORD_REPORT=1 uv run pytest tests/unit/renderers/test_report_golden.py

Recording never overwrites (it only writes a case directory that does not exist yet) and refuses to
run in CI (`CI` set): goldens are recorded deliberately, by hand, and committed.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from edisc_core.canonical import canonical_json
from edisc_renderers.report import html as rhtml
from edisc_renderers.report import model as m
from edisc_renderers.report.version import REPORT_RENDERER_VERSION

GOLDEN = pathlib.Path(__file__).parents[2] / "golden" / "report"
T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
SEALED = datetime(2026, 1, 6, 12, 34, 56, tzinfo=UTC)

# names and error texts that exercise the sanitiser (S1's hard strings, the classes of fix 2, and a
# user string that literally reads like a marker: fix 1); no lone surrogate (RFC 8785 cannot hold
# one in report.json, which refuses it: test_report_html)
HARD = [
    "张伟（销售部）", "山田太郎・営業", "김민준 팀장", "محمد عبد الله", "שרה כהן",  # noqa: RUF001
    "Report: تقرير 2026 — שלב 3", "🎉 launch 👍🏽 🇮🇳", "👩‍👩‍👧‍👦 family", "é̂ Zalgo z̶a̷l̸",
    "नमस्ते क्षत्रिय", "สวัสดีครับ", "zero​width‌joiner﻿bom", "‮evil.exe‬ override",
    "Linear B \U00010000 and \U00013000", "literal [U+202E] text", "nul\x00 c0\x01 del\x7f c1\x85",
    "nonchar ﷐ ￿ tab\tcr\rlf\n", "<script>alert(1)</script> & \"'",
]  # fmt: skip


def key() -> str:
    return f"{REPORT_RENDERER_VERSION}_unicode-{unicodedata.unidata_version}"


def recording() -> bool:
    if os.environ.get("EDISC_RECORD_REPORT") != "1":
        return False
    if os.environ.get("CI"):
        pytest.fail("goldens are never recorded in CI: record by hand and commit them")
    return True


def _digest(name: str, rows: int) -> m.JsonlDigest:
    d = m.JsonlDigest(name)
    for i in range(rows):
        d.add(m.jsonl_line({"row": i, "file": name}))
    return d


def _inputs(*, units: int, conversations: int, status: str = "completed", live: bool = True,
            failed_every: int = 0, gap_every: int = 0, hard: bool = False,
            custody_ok: bool = True, divergences: int = 0,
            retention_gaps: int = 0) -> m.ReportInputs:  # fmt: skip
    """Fixed report inputs (no randomness, no clock): ``units`` units spread over
    ``conversations`` conversations, every ``failed_every``-th unit failed and every
    ``gap_every``-th one with a gap; ``hard`` puts the hard strings in names and error texts."""
    chain = m.ChainFold()
    started: dict[str, Any] = {"connector": "dummy", "connector_version": "0.4.0",
                               "connection_id": "conn-1", "scopes": [{"type": "channel",
                               "external_id": "C0", "from": "2026-01-01", "to": "2026-01-31"}]}  # fmt: skip
    if live:
        started |= {"plan_tier": "pro", "granted_scopes": ["channels:history"],
                    "blind_spots": ["private channels"], "unit_day_zone": "UTC"}  # fmt: skip
    chain.add(m.ChainEvent(1, "job_started", "user:alice", T0, started))
    capped, conv_capped, fold = m.Capped(), m.Capped(), m.ConversationFold()
    status_counts: dict[str, int] = {}
    recon_counts: dict[str, int] = {}
    totals = {"expected": 0, "collected": 0, "file_gaps": 0}
    units_file = m.JsonlDigest("units.jsonl")
    per_conv = max(1, units // max(1, conversations))
    for i in range(units):
        conv = f"C{i // per_conv:05d}" + (f" {HARD[(i // per_conv) % len(HARD)]}" if hard else "")
        key_ = f"{conv}/2026-01-{1 + i % per_conv % 28:02d}"
        failed = bool(failed_every) and i % failed_every == 0
        gap = not failed and bool(gap_every) and i % gap_every == 0
        recon = "failed" if failed else ("gap" if gap else "matched")
        error = (
            (HARD[(i * 7) % len(HARD)] if hard else "HTTP 429 after 6 retries") if failed else None
        )
        row = {"unit_key": key_, "kind": "conversation_day", "conversation_id": conv,
               "day": key_[-10:], "zone": "UTC", "scopes": [0], "access_lost_reason": None,
               "divergent": False, "source": "job_chain", "status": "failed" if failed else "done",
               "recon_status": recon, "expected": None if failed else 5,
               "collected": None if failed else (4 if gap else 5),
               "file_gaps": None if failed else 0, "no_longer_observed": None if failed else 0,
               "basis": None, "day_anomalies": None, "error_type": "SourceError" if failed else None,
               "error": error}  # fmt: skip
        units_file.add(m.jsonl_line(row))
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        recon_counts[recon] = recon_counts.get(recon, 0) + 1
        for k in totals:
            totals[k] += int(row[k] or 0)
        if recon in m.EXCEPTION_RECON:
            capped.add(recon, m.unit_order(row), row)
        done = fold.add(row)
        if done is not None:
            conv_capped.add(done["worst_status"], (done["conversation_id"],), done)
    last = fold.finish()
    if last is not None:
        conv_capped.add(last["worst_status"], (last["conversation_id"],), last)
    chain.add(m.ChainEvent(2, "job_finished", "system", SEALED, {"status": status}))
    obs = _digest("observations.jsonl", 0)
    files = [units_file, obs, _digest("renders.jsonl", 0)]
    conv_file = None
    if conv_capped.total > m.CAP:
        conv_file = _digest("conversations.jsonl", conv_capped.total)
        files.append(conv_file)
    unit_rec = units_file.record()
    return m.ReportInputs(
        job={"id": "0192f3a4-7c1e-7d2a-9b5e-3f0c2d1e4a5b", "status": status,
             "sealed_at": m.iso(SEALED)},
        chain=chain, verification={"ok": custody_ok, "errors": [] if custody_ok else ["broken"]},
        snapshot={"renders": [], "digest": "3f9a1c0d" * 8,
                  "retention_gaps": [{"evidence_object_id": f"e{i}"} for i in range(retention_gaps)]},
        access={"connection_source": "dummy"}, unit_status_counts=status_counts,
        recon_counts=recon_counts, totals=totals,
        exceptions={"units": capped.record(unit_rec),
                    "observations_capped": m.Capped().record(obs.record()),
                    "unavailable_files_by_reason": [], "evidence_not_complete": 0},
        observation_counts={}, pauses_db=[],
        divergences=[m.Divergence("unit_differs", f"u{i}", 1, 2) for i in range(divergences)],
        normalizer_versions=["0.2.0"],
        versions={"report_renderer": REPORT_RENDERER_VERSION, "unicode": unicodedata.unidata_version,
                  "pdf_toolchain": None, "worker_image": m.UNKNOWN},
        audit_events=[], files=files, evidence={"objects": units, "not_complete": 0},
        identity={"paper": "letter"},
        conversations=m.conversations_section(conv_capped, conv_file and conv_file.record()),
    )  # fmt: skip


CASES: dict[str, Callable[[], m.ReportInputs]] = {
    "clean": lambda: _inputs(units=12, conversations=3),
    "gaps_and_failures_hard_strings": lambda: _inputs(
        units=40, conversations=8, status="completed_with_gaps", failed_every=5, gap_every=3,
        hard=True),
    "archive": lambda: _inputs(units=6, conversations=2, status="completed_against_archive"),
    "pre_change_unknown": lambda: _inputs(units=4, conversations=1, live=False),
    "custody_failed_divergent": lambda: _inputs(
        units=4, conversations=1, custody_ok=False, divergences=2, retention_gaps=1),
    # at the caps: 1,205 unit exceptions and 1,001 conversations, the remainders named by file
    "at_the_cap": lambda: _inputs(
        units=2_002, conversations=1_001, status="failed", failed_every=2, gap_every=3, hard=True),
}  # fmt: skip


def build(name: str) -> dict[str, bytes]:
    doc = m.report_document(CASES[name]())
    return {"report.json": canonical_json(doc), "report.html": rhtml.report_html(doc)}


def _index(files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "renderer_version": REPORT_RENDERER_VERSION,
        "unicode": unicodedata.unidata_version,
        "files": [
            {"name": n, "sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
            for n, b in sorted(files.items())
        ],
    }


@pytest.mark.parametrize("name", sorted(CASES))
def test_report_golden_bytes(name: str) -> None:
    files = build(name)
    index = _index(files)
    directory = GOLDEN / key() / name
    if not directory.exists():
        if not recording():
            pytest.fail(
                f"no report goldens for {key()}/{name}; record them with EDISC_RECORD_REPORT=1 (only"
                " after a deliberate REPORT_RENDERER_VERSION bump or a new Python/Unicode pin)"
            )
        directory.mkdir(parents=True)
        for file_name, data in files.items():
            (directory / file_name).write_bytes(data)
        (directory / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        return
    recording()  # refuses in CI; an existing generation is only compared, never rewritten
    recorded = json.loads((directory / "index.json").read_text())
    assert index == recorded, (
        f"report output changed under renderer version {REPORT_RENDERER_VERSION}: bump "
        "REPORT_RENDERER_VERSION and record a new golden generation"
    )
    assert sorted(p.name for p in directory.iterdir()) == sorted([*files, "index.json"])
    for file_name, data in files.items():
        assert (directory / file_name).read_bytes() == data, file_name


def test_the_current_generation_has_every_case() -> None:
    """Old generations stay as history; the current one must exist (no silent skip)."""
    assert (GOLDEN / key()).is_dir(), f"record {key()} with EDISC_RECORD_REPORT=1"
    assert sorted(p.name for p in (GOLDEN / key()).iterdir()) == sorted(CASES)


def test_the_cap_case_names_both_remainders() -> None:
    doc = json.loads(build("at_the_cap")["report.json"])
    assert doc["exceptions"]["units"]["more"] > 0 and doc["exceptions"]["units"]["more_in"]
    assert doc["conversations"]["total"] == 1_001
    assert doc["conversations"]["more_in"]["name"] == "conversations.jsonl"
