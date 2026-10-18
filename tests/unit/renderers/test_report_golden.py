"""Report goldens: `report.json` and `report.html` bytes per renderer version (ADR 0018 §3, §15; fix 4
before step 3). A regression guard, like the RSMF goldens: correctness comes from the oracle and the
sanitiser tests; a golden only says "the bytes did not change". They are also the INPUT of the PDF
goldens (step 3): a PDF golden is the PDF of a recorded `report.html` here, so a PDF golden can only
be recorded once the HTML it renders is pinned.

`tests/golden/report/<key>/<case>/` holds `report.json`, `report.html` and `index.json`. The key is
`<REPORT_RENDERER_VERSION>_unicode-<Unicode version>_glyphs-<glyph set 12>` (every input to byte
identity besides the data: the glyph set decides which characters become markers, ADR 0018 §24;
the inputs below are fixed literals, not generated). Changing the output bytes without bumping
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
from datetime import UTC, datetime, timedelta
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
# user string that literally reads like a marker: fix 1) and lone surrogates (report.json writes
# them as replacement text plus code units, the HTML as markers: ADR 0018 §23)
HARD = [
    "张伟（销售部）", "山田太郎・営業", "김민준 팀장", "محمد عبد الله", "שרה כהן",  # noqa: RUF001
    "Report: تقرير 2026 — שלב 3", "🎉 launch 👍🏽 🇮🇳", "👩‍👩‍👧‍👦 family", "é̂ Zalgo z̶a̷l̸",
    "नमस्ते क्षत्रिय", "สวัสดีครับ", "zero​width‌joiner﻿bom", "‮evil.exe‬ override",
    "Linear B \U00010000 and \U00013000", "literal [U+202E] text", "nul\x00 c0\x01 del\x7f c1\x85",
    "nonchar ﷐ ￿ tab\tcr\rlf\n", "<script>alert(1)</script> & \"'",
]  # fmt: skip
# lone surrogates (§23): report.json writes them as replacement text plus code units, the HTML as
# markers. Not in HARD's rotation (which the PDF tests rely on): the first failed unit's error.
LONE = "lone \ud83d high, \udc00 low, \ude00\ud83d reversed"


# what `job_started.export` records for a free-plan export (§32); one sample entry name is hostile
EXPORT: dict[str, Any] = {
    "id": "0192f3a4-0000-7000-8000-00000000e0e0", "sha256": "5e" * 32, "size_bytes": 1_234_567,
    "entry_count": 42, "declared_plan": "free", "detected_tier": "public_only",
    "tier_confirmed": True,
    "blind_spots": [
        "Edits keep only the latest text; earlier versions are not in the export.",
        "Private channels, direct messages and group direct messages are not in this export.",
    ],
    "tier_warnings": ["users.json is missing: authors cannot be resolved to user profiles."],
    "findings": {
        "entries_by_kind": {"day_file": 36, "metadata": 2, "unknown": 1},
        "unknown_entries": {"count": 1, "sample": [HARD[12]]},
        "metadata_files": ["channels.json", "users.json"],
        "conversations_refused": {}, "messages": 120, "workspace_id": None,
    },
}  # fmt: skip


def key() -> str:
    return (
        f"{REPORT_RENDERER_VERSION}_unicode-{unicodedata.unidata_version}"
        f"_glyphs-{rhtml.glyph_set_id()[:12]}"
    )


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
            retention_gaps: int = 0, export: bool = False,
            pauses: str | None = None) -> m.ReportInputs:  # fmt: skip
    """Fixed report inputs (no randomness, no clock): ``units`` units spread over
    ``conversations`` conversations, every ``failed_every``-th unit failed and every
    ``gap_every``-th one with a gap; ``hard`` puts the hard strings in names and error texts;
    ``pauses`` adds pauses and how they ended (ADR 0016 A6): ``finished`` (re-authorized once, then
    paused again and finished without access, with a hard-string reason) or ``deadline``."""
    chain = m.ChainFold()
    started: dict[str, Any] = {"connector": "dummy", "connector_version": "0.4.0",
                               "connection_id": "conn-1", "scopes": [{"type": "channel",
                               "external_id": "C0", "from": "2026-01-01", "to": "2026-01-31"}]}  # fmt: skip
    if live:
        started |= {"plan_tier": "pro", "granted_scopes": ["channels:history"],
                    "blind_spots": ["private channels"], "unit_day_zone": "UTC"}  # fmt: skip
    if export:  # a free-plan export: public channels only (§32)
        started |= {"plan_tier": "public_only", "export": EXPORT}
    chain.add(m.ChainEvent(1, "job_started", "user:alice", T0, started))
    for event in _pause_events(pauses):
        chain.add(event)
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
        if hard and failed and i == 0:
            error = LONE
        # §28: the chain states scopes, access-lost and gap reasons (UNKNOWN before it recorded them)
        detail: dict[str, Any] = (
            {"scopes": [0], "access_lost_reason": None,
             "gap_reasons": ["thread_ts_lone_surrogate"] if gap and i % 2 else []}
            if live else
            {"scopes": m.UNKNOWN, "access_lost_reason": m.UNKNOWN, "gap_reasons": m.UNKNOWN}
        )  # fmt: skip
        row = {"unit_key": key_, "kind": "conversation_day", "conversation_id": conv,
               "day": key_[-10:], "zone": "UTC", **detail,
               "divergent": False, "source": "job_chain", "status": "failed" if failed else "done",
               "recon_status": recon, "expected": None if failed else 5,
               "collected": None if failed else (4 if gap else 5),
               "file_gaps": None if failed else 0, "no_longer_observed": None if failed else 0,
               "basis": None, "day_anomalies": None, "error_type": "SourceError" if failed else None,
               "error": error}  # fmt: skip
        units_file.row(row)
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


def _pause_events(kind: str | None) -> list[m.ChainEvent]:
    def paused(seq: int, hours: int) -> m.ChainEvent:
        at = T0 + timedelta(hours=hours)
        return m.ChainEvent(seq, "job_paused", "system", at, {
            "reason": "invalid_auth (credentials_revoked)", "connection_id": "conn-1",
            "reason_class": "credentials_revoked",
            "deadline_at": (at + timedelta(hours=168)).isoformat()})  # fmt: skip

    def ended(seq: int, hours: int, how: str, actor: str, reason: str | None) -> m.ChainEvent:
        return m.ChainEvent(seq, "job_pause_ended", actor, T0 + timedelta(hours=hours), {
            "ended_how": how, "reason_class": "credentials_revoked",
            **({"reason": reason} if reason is not None else {})})  # fmt: skip

    if kind == "finished":
        return [
            paused(10, 1),
            m.ChainEvent(
                11, "job_resumed", "user:dana", T0 + timedelta(hours=3), {"connection_id": "conn-1"}
            ),
            paused(12, 5),
            ended(
                13,
                9,
                "finished_without_access",
                "user:sam",
                f"custodian left the firm {HARD[12]} {HARD[5]}",
            ),
        ]
    if kind == "deadline":
        return [paused(10, 1), ended(11, 169, "deadline", "system:access-lost", None)]
    return []


CASES: dict[str, Callable[[], m.ReportInputs]] = {
    "clean": lambda: _inputs(units=12, conversations=3),
    "gaps_and_failures_hard_strings": lambda: _inputs(
        units=40, conversations=8, status="completed_with_gaps", failed_every=5, gap_every=3,
        hard=True),
    "archive": lambda: _inputs(
        units=6, conversations=2, status="completed_against_archive", export=True),
    "pre_change_unknown": lambda: _inputs(units=4, conversations=1, live=False, gap_every=2),
    "custody_failed_divergent": lambda: _inputs(
        units=4, conversations=1, custody_ok=False, divergences=2, retention_gaps=1),
    # at the caps: 1,205 unit exceptions and 1,001 conversations, the remainders named by file
    # how pauses ended (ADR 0016 A6, report renderer 1.6.0)
    "pauses_reauthorized_then_finished": lambda: _inputs(
        units=6, conversations=2, status="completed_with_gaps", pauses="finished"),
    "pause_deadline": lambda: _inputs(
        units=4, conversations=1, status="completed_with_gaps", pauses="deadline"),
    "at_the_cap": lambda: _inputs(
        units=2_002, conversations=1_001, status="failed", failed_every=2, gap_every=3, hard=True),
}  # fmt: skip


def build(name: str) -> dict[str, bytes]:
    doc = m.valid_document(m.report_document(CASES[name]()))
    return {"report.json": canonical_json(doc), "report.html": rhtml.report_html(doc)}


def _index(files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "renderer_version": REPORT_RENDERER_VERSION,
        "unicode": unicodedata.unidata_version,
        "glyph_set": rhtml.glyph_set_id(),
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
