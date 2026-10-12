"""The clean verdict and the normalizer versions, recomputed from the chain alone (ADR 0018 §30): a
report may never state clean when the chain does not give clean (the recomputation fails, even for a
report that is otherwise consistent); a report stating not clean for a chain that gives clean passes,
with the reasons it states; the normalizer versions `job_started` and each batch record are what the
report states, and a job started before they were recorded says so."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from edisc_core.canonical import canonical_json
from edisc_renderers.report import model as m
from edisc_renderers.report.recompute import (
    NORMALIZERS_NOT_RECORDED,
    chain_verdict,
    fold_chain,
    recompute,
)

from .test_report_recompute import _detail, _report

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _events(
    *,
    status: str = "completed",
    units: tuple[tuple[str, str, int], ...] = (("C1", "matched", 5), ("C2", "matched", 5)),
    normalizers: list[str] | None = None,
    batch_normalizer: str | None = None,
    extra: tuple[tuple[str, dict[str, Any]], ...] = (),
) -> list[dict[str, Any]]:
    started: dict[str, Any] = {"connector": "dummy", "unit_day_zone": "UTC",
                               "scopes": [{"type": "channel", "id": "*"}],
                               "unit_event_format": m.DETAILED_FORMAT}  # fmt: skip
    if normalizers is not None:
        started["normalizer_versions"] = normalizers
    out: list[tuple[str, dict[str, Any]]] = [("job_started", started)]
    batch: dict[str, Any] = {"unit_key": "C1/2026-01-05", "item_count": 1}
    if batch_normalizer is not None:
        batch["normalizer_version"] = batch_normalizer
    out.append(("items_collected", batch))
    for conv, recon, collected in units:
        out.append(("unit_reconciled", {"unit_key": f"{conv}/2026-01-05", "expected": 5,
                    "collected": collected, "recon_status": recon, "file_gaps": 0,
                    "no_longer_observed": 0, **_detail(conv)}))  # fmt: skip
    out += list(extra)
    out.append(("job_finished", {"status": status}))
    return [
        {"fields": {"seq": i + 1, "event_type": t, "actor": "system",
                    "created_at": T0.isoformat(), "payload": p}}
        for i, (t, p) in enumerate(out)
    ]  # fmt: skip


def _forged_clean(report_json: bytes) -> bytes:
    """The report's own verdict flipped to clean, everything else as written."""
    doc = json.loads(report_json)
    doc["job"]["clean"] = True
    doc["banner"] = [m.CLEAN_BANNER]
    return canonical_json(doc)


def test_a_clean_chain_and_a_clean_report_agree() -> None:
    events = _events()
    report_json, units = _report(events)
    assert json.loads(report_json)["job"]["clean"] is True
    done = recompute(events, report_json, units)
    assert done.ok, done.differences
    assert (done.chain_clean, done.stated_clean, done.stated_not_clean) == (True, True, [])


def test_a_report_stating_clean_for_a_chain_that_is_not_fails() -> None:
    events = _events(status="completed_with_gaps", units=(("C1", "matched", 5), ("C2", "gap", 3)))
    report_json, units = _report(events)
    assert json.loads(report_json)["job"]["clean"] is False
    assert recompute(events, report_json, units).ok  # as written: consistent
    done = recompute(events, _forged_clean(report_json), units)
    assert not done.ok
    (verdict,) = [d for d in done.differences if d.startswith("verdict:")]
    assert "final status completed_with_gaps" in verdict and "1 unit(s) gap" in verdict
    # the banner alone stating clean is enough to fail (the HTML and PDF print it)
    doc = json.loads(report_json)
    doc["banner"] = [m.CLEAN_BANNER]
    assert any(
        d.startswith("verdict:") for d in recompute(events, canonical_json(doc), units).differences
    )


def test_a_forged_clean_report_of_a_consistent_looking_chain_fails() -> None:
    """The final event says `completed`, but a unit the chain states is a gap: the chain contradicts
    its own status, so it does not give clean, and a report stating clean (rows, counts and sections
    all consistent with the chain) fails on the verdict alone."""
    events = _events(units=(("C1", "matched", 5), ("C2", "gap", 3)))
    report_json, units = _report(events)
    forged = _forged_clean(report_json)
    done = recompute(events, forged, units)
    assert [d for d in done.differences if not d.startswith("verdict:")] == []
    assert done.differences == [
        "verdict: the report states clean, the chain does not give clean: 1 unit(s) gap"
    ]


def test_a_report_stating_not_clean_for_a_clean_chain_passes_with_its_reasons() -> None:
    events = _events()
    report_json, units = _report(
        events,
        divergences=[m.Divergence("unit_differs", "C1/2026-01-05", {"a": 1}, {"a": 2})],
        retention_gaps=[{"evidence_object_id": "x", "unprotected_from": T0.isoformat()}],
    )
    doc = json.loads(report_json)
    assert doc["job"]["clean"] is False
    # the divergent flag of C1's row comes from the report's own divergence list
    done = recompute(events, report_json, units)
    assert done.ok, done.differences
    assert done.chain_clean and not done.stated_clean
    assert done.stated_not_clean == [
        "1 divergence(s) between the chain and the database: unit_differs 1",
        "1 retention gap(s) touching the job's evidence",
    ]


def test_the_chain_verdict_names_every_reason() -> None:
    cancel = ("cancel_requested", {"reason": "user"})
    for events, reason in [
        (_events(status="completed_against_archive"), "final status completed_against_archive"),
        (_events(status="completed_unverified", units=(("C1", "unverifiable", 5),)),
         "1 unit(s) unverifiable"),
        (_events(extra=(cancel,)), "cancel_requested recorded (seq 5)"),
        (_events(units=(("C1", "access_lost", 0),)), "1 unit(s) access_lost"),
    ]:  # fmt: skip
        fold, facts = fold_chain(events)
        reasons = chain_verdict(fold, facts)
        assert any(r.startswith(reason) for r in reasons), (reason, reasons)
    fold, facts = fold_chain(_events()[:-1])  # no final event
    assert "the chain does not hold exactly one final event" in chain_verdict(fold, facts)
    fold, facts = fold_chain(_events())
    assert chain_verdict(fold, facts) == []


def test_normalizer_versions_are_stated_from_the_chain_and_recomputed() -> None:
    events = _events(normalizers=["0.3.0"], batch_normalizer="0.4.0")  # resumed by an upgrade
    fold, _ = fold_chain(events)
    assert fold.normalizer_versions == ["0.3.0", "0.4.0"]
    report_json, units = _report(events)
    assert json.loads(report_json)["versions"]["normalizer_versions"] == ["0.3.0", "0.4.0"]
    done = recompute(events, report_json, units)
    assert done.ok and done.normalizers_recorded, done.differences
    assert NORMALIZERS_NOT_RECORDED not in done.not_recomputed()
    doc = json.loads(report_json)
    doc["versions"]["normalizer_versions"] = ["0.3.0"]
    done = recompute(events, canonical_json(doc), units)
    assert any(d.startswith("versions:") for d in done.differences), done.differences


def test_a_job_started_before_normalizer_versions_were_recorded_says_so() -> None:
    events = _events()  # no normalizer_versions in job_started
    fold, _ = fold_chain(events)
    assert fold.normalizer_versions is None
    report_json, units = _report(events)
    done = recompute(events, report_json, units)
    assert done.ok and not done.normalizers_recorded
    assert NORMALIZERS_NOT_RECORDED in done.not_recomputed()
