"""The report's unit facts from the chain and their offline recomputation (ADR 0018 §28), pure: the
format is decided once per job by `job_started`, rows state the chain's details (UNKNOWN before they
were recorded), and `recompute` rebuilds `units.jsonl` byte for byte and the sections derived from it,
naming any difference."""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from typing import Any

from edisc_core.canonical import canonical_json
from edisc_renderers.report import model as m
from edisc_renderers.report.recompute import recompute
from edisc_renderers.report.version import REPORT_RENDERER_VERSION
from edisc_worker import pipeline

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def test_the_model_and_the_pipeline_agree_on_the_constants() -> None:
    assert m.DIRECTORY_UNIT_KEY == pipeline.DIRECTORY_UNIT
    assert m.DETAILED_FORMAT == pipeline.UNIT_EVENT_FORMAT


def _detail(conv: str, gaps: list[str] | None = None) -> dict[str, Any]:
    kind = "directory" if conv == "" else "conversation_day"
    return {"kind": kind, "conversation_id": conv, "scopes": [] if conv == "" else [0],
            "access_lost_reason": None, "gap_reasons": gaps or []}  # fmt: skip


def _events(*, detailed: bool = True) -> list[dict[str, Any]]:
    started: dict[str, Any] = {"connector": "dummy", "unit_day_zone": "UTC",
                               "scopes": [{"type": "channel", "id": "*"}]}  # fmt: skip
    if detailed:
        started["unit_event_format"] = m.DETAILED_FORMAT
    out = [("job_started", started)]
    for key, conv, recon, collected, gaps in [
        ("C2/2026-01-05", "C2", "gap", 3, ["thread_ts_lone_surrogate"]),
        ("C1/2026-01-05", "C1", "matched", 5, []),
        ("directory", "", "not_applicable", 0, []),
    ]:  # fmt: skip
        out.append(("unit_reconciled", {"unit_key": key, "expected": 5, "collected": collected,
                    "recon_status": recon, "file_gaps": 0, "no_longer_observed": 0,
                    **_detail(conv, gaps)}))  # fmt: skip
    out.append(("units_unsettled", {"units": [{"unit_key": "C1/2026-01-06", "status": "pending",
                "recon_status": "pending", "expected": None, "collected": 0, "file_gaps": 0,
                **_detail("C1")}]}))  # fmt: skip
    out.append(("job_cancelled", {"status": "cancelled"}))
    return [
        {
            "fields": {
                "seq": i + 1,
                "event_type": t,
                "actor": "system",
                "created_at": T0.isoformat(),
                "payload": p,
            }
        }
        for i, (t, p) in enumerate(out)
    ]


def _report(events: list[dict[str, Any]]) -> tuple[bytes, bytes]:
    """`report.json` and `units.jsonl` as the loader writes them (a database agreeing with the
    chain), built here from the model's row and section functions."""
    fold = m.ChainFold()
    facts: dict[str, m.UnitFact] = {}
    for rec in events:
        f = rec["fields"]
        for fact in fold.add(m.ChainEvent(f["seq"], f["event_type"], f["actor"], T0, f["payload"])):
            facts.setdefault(fact.unit_key, fact)
    rows = sorted(
        (m.unit_row({"unit_key": k}, stated=v, zone="UTC", scopes=(), divergent=False)
         for k, v in facts.items()),
        key=m.unit_order,
    )  # fmt: skip
    units = m.JsonlDigest("units.jsonl")
    body = b"".join(units.row(r) for r in rows)
    status_counts: dict[str, int] = {}
    recon_counts: dict[str, int] = {}
    totals = {"expected": 0, "collected": 0, "file_gaps": 0}
    capped, conv_capped, convs = m.Capped(), m.Capped(), m.ConversationFold()
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        if r["kind"] != "directory":
            recon_counts[r["recon_status"]] = recon_counts.get(r["recon_status"], 0) + 1
        for k in totals:
            totals[k] += int(r[k] or 0)
        if m.unit_severity(r) in m.EXCEPTION_RECON:
            capped.add(m.unit_severity(r), m.unit_order(r), r)
        done = convs.add(r)
        if done is not None:
            conv_capped.add(done["worst_status"], (done["conversation_id"],), done)
    last = convs.finish()
    if last is not None:
        conv_capped.add(last["worst_status"], (last["conversation_id"],), last)
    obs = m.JsonlDigest("observations.jsonl")
    doc = m.report_document(m.ReportInputs(
        job={"status": "cancelled"}, chain=fold, verification={"ok": True, "errors": []},
        snapshot={"renders": [], "digest": "00" * 32, "retention_gaps": []}, access={},
        unit_status_counts=status_counts, recon_counts=recon_counts, totals=totals,
        exceptions={"units": capped.record(units.record()),
                    "observations_capped": m.Capped().record(obs.record())},
        observation_counts={}, pauses_db=[], divergences=[], normalizer_versions=[],
        versions={"report_renderer": REPORT_RENDERER_VERSION,
                  "unicode": unicodedata.unidata_version},
        audit_events=[], files=[units, obs], evidence={}, identity={},
        conversations=m.conversations_section(conv_capped, None),
    ))  # fmt: skip
    return canonical_json(m.valid_document(doc)), body


def test_a_detailed_chain_recomputes_byte_for_byte() -> None:
    events = _events()
    report_json, units = _report(events)
    done = recompute(events, report_json, units)
    assert done.ok, done.differences
    assert (done.units_rows, done.database_rows) == (4, 0)
    rows = {r["unit_key"]: r for r in map(json.loads, units.splitlines())}
    assert rows["C2/2026-01-05"]["gap_reasons"] == ["thread_ts_lone_surrogate"]
    assert rows["C1/2026-01-06"]["status"] == "pending"  # unsettled, from the chain
    assert rows["directory"]["conversation_id"] is None and rows["directory"]["scopes"] == []


def test_before_the_format_the_chain_states_unknown_and_ignores_later_details() -> None:
    events = _events(detailed=False)
    fold = m.ChainFold()
    facts = [f for rec in events for f in fold.add(m.ChainEvent(
        rec["fields"]["seq"], rec["fields"]["event_type"], "system", T0, rec["fields"]["payload"]))]  # fmt: skip
    assert facts and all(f.detail is None for f in facts)
    assert not any(f.outcome == "unsettled" for f in facts)  # read as before §28
    row = m.unit_row({"unit_key": "C1/2026-01-05"}, stated=facts[1], zone="UTC", scopes=(),
                     divergent=False)  # fmt: skip
    assert (row["scopes"], row["access_lost_reason"], row["gap_reasons"]) == (m.UNKNOWN,) * 3
    assert (row["kind"], row["conversation_id"]) == ("conversation_day", "C1")
    report_json, units = _report(events)
    assert recompute(events, report_json, units).ok


def test_the_recomputation_names_what_differs() -> None:
    events = _events()
    report_json, units = _report(events)
    # a units.jsonl row stating other scopes than the chain recorded
    lines = units.splitlines(keepends=True)
    at = next(i for i, line in enumerate(lines) if b'"scopes":[0]' in line)
    lines[at] = lines[at].replace(b'"scopes":[0]', b'"scopes":[1]')
    done = recompute(events, report_json, b"".join(lines))
    assert any("scopes: written [1], recomputed [0]" in d for d in done.differences), done
    # report.json counting another number of units by status
    doc = json.loads(report_json)
    doc["counts"]["units_by_status"][0]["count"] += 1
    done = recompute(events, canonical_json(doc), units)
    assert any(d.startswith("counts:") for d in done.differences), done.differences
    # another unit day zone than the chain's
    doc = json.loads(report_json)
    doc["job"]["unit_day_zone"] = "Europe/Paris"
    assert any(
        d.startswith("job:") for d in recompute(events, canonical_json(doc), units).differences
    )
    # a row missing from units.jsonl
    done = recompute(events, report_json, b"".join(units.splitlines(keepends=True)[1:]))
    assert any("units.jsonl: the file is not what the chain gives" in d for d in done.differences)
