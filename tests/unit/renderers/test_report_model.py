"""The pure collection report model (ADR 0018 §4, §8, §12)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from edisc_core.schemas import ARCHIVE_CAVEAT, JobStatus, ReconStatus, UnitStatus
from edisc_renderers.report import model as m

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
# ADR 0018 §4.6, worst first, written out here so the test does not borrow the code's constant
ADR_SEVERITY = (
    "failed", "access_lost", "gap", "surplus", "unverifiable", "matched_against_archive",
    "pending", "matched", "not_applicable",
)  # fmt: skip


def _reconciled(key: str = "C1/2026-01-05", **kw: object) -> dict[str, object]:
    return {"unit_key": key, "expected": 3, "collected": 3, "recon_status": "matched",
            "file_gaps": 0, "no_longer_observed": 0, **kw}  # fmt: skip


def _row(key: str = "C1/2026-01-05", **kw: object) -> dict[str, object]:
    return {"unit_key": key, "kind": "conversation_day", "conversation_id": key.split("/")[0],
            "status": "done", "recon_status": "matched", "expected_count": 3,
            "collected_count": 3, "file_gaps": 0, "day_anomalies": 0, "last_error": None, **kw}  # fmt: skip


def test_the_database_row_of_a_reconciled_unit_matches_its_chain_fact() -> None:
    chain = m.unit_fact_from_event("unit_reconciled", _reconciled())
    db = m.unit_fact_from_row(_row(), no_longer_observed=0, archive_backed=False)
    assert chain == db


def test_archive_units_carry_basis_and_anomalies_on_both_sides() -> None:
    chain = m.unit_fact_from_event(
        "unit_reconciled",
        _reconciled(recon_status="matched_against_archive", basis="archive", day_anomalies=2),
    )
    db = m.unit_fact_from_row(
        _row(recon_status="matched_against_archive", day_anomalies=2),
        no_longer_observed=0, archive_backed=True,
    )  # fmt: skip
    assert chain == db


def test_a_failed_unit_compares_its_error_across_the_truncations() -> None:
    error = "x" * 3_000
    chain = m.unit_fact_from_event(
        "unit_failed", {"unit_key": "C1/2026-01-05", "error_type": "Boom", "error": error[:2000]}
    )
    db = m.unit_fact_from_row(
        _row(status="failed", recon_status="failed", last_error=f"Boom: {error}"[:4000]),
        no_longer_observed=0, archive_backed=False,
    )  # fmt: skip
    assert chain == db


def test_an_unsettled_unit_has_no_fact_yet() -> None:
    for status in ("pending", "running", "retry_later", "paused"):
        assert (
            m.unit_fact_from_row(_row(status=status), no_longer_observed=0, archive_backed=False)
            is None
        )


def test_digests_agree_when_the_records_agree_and_name_the_differing_buckets() -> None:
    chain, db = m.DigestFold(), m.DigestFold()
    for i in range(50):
        key = f"C{i}/2026-01-05"
        chain.add(m.unit_fact_from_event("unit_reconciled", _reconciled(key)))
        db.add(m.unit_fact_from_row(_row(key), no_longer_observed=0, archive_backed=False))  # type: ignore[arg-type]
    assert chain.differing(db) == [] and chain.digest == db.digest
    altered = m.DigestFold()
    for i in range(50):
        key = f"C{i}/2026-01-05"
        altered.add(
            m.unit_fact_from_row(
                _row(key, collected_count=2 if i == 7 else 3),
                no_longer_observed=0,
                archive_backed=False,
            )  # type: ignore[arg-type]
        )
    assert chain.differing(altered) == [m.bucket_of("C7/2026-01-05")]


def test_compare_units_names_every_kind_of_disagreement_and_states_the_chain() -> None:
    f = lambda key, **kw: m.unit_fact_from_event("unit_reconciled", _reconciled(key, **kw))  # noqa: E731
    divergences, stated = m.compare_units(
        [f("a/2026-01-01"), f("b/2026-01-01"), f("b/2026-01-01"), f("c/2026-01-01")],
        [f("a/2026-01-01", collected=2), f("b/2026-01-01"), f("d/2026-01-01")],
    )
    kinds = {(d.kind, d.subject) for d in divergences}
    assert kinds == {
        ("unit_differs", "a/2026-01-01"),
        ("duplicate_unit_event", "b/2026-01-01"),
        ("unit_unsettled_in_database", "c/2026-01-01"),
        ("unit_missing_from_chain", "d/2026-01-01"),
    }
    assert stated["a/2026-01-01"].collected == 3  # the chain value is the fact
    assert "d/2026-01-01" not in stated


@pytest.mark.parametrize("status", [s.value for s in JobStatus])
def test_only_a_completed_job_with_clean_custody_and_records_is_clean(status: str) -> None:
    assert m.clean(status, custody_ok=True, divergences=0, retention_gaps=0) is (
        status == "completed"
    )
    assert m.clean("completed", custody_ok=False, divergences=0, retention_gaps=0) is False
    assert m.clean("completed", custody_ok=True, divergences=1, retention_gaps=0) is False
    assert m.clean("completed", custody_ok=True, divergences=0, retention_gaps=1) is False


def test_banners_lead_with_custody_and_records_and_quote_the_caveat() -> None:
    lines = m.banner("completed_against_archive", custody_ok=True, divergences=0, units={},
                     unverifiable=0, is_clean=False)  # fmt: skip
    assert lines == ["COMPLETE RELATIVE TO THE PROVIDED EXPORT ONLY", ARCHIVE_CAVEAT]
    lines = m.banner("completed", custody_ok=False, divergences=2, units={}, unverifiable=0,
                     is_clean=False)  # fmt: skip
    assert lines[0] == "CUSTODY VERIFICATION FAILED" and lines[1].startswith("RECORDS DISAGREE")
    assert m.banner("completed_unverified", custody_ok=True, divergences=0, units={},
                    unverifiable=4, is_clean=False) == [
        "NOT VERIFIED: 4 units could not be checked against a source count"
    ]  # fmt: skip
    assert m.banner("completed_with_gaps", custody_ok=True, divergences=0, units={"gap": 2},
                    unverifiable=0, is_clean=False)[0].startswith("NOT COMPLETE")  # fmt: skip


@given(st.lists(st.tuples(st.sampled_from(ADR_SEVERITY), st.text(max_size=4)), max_size=60))
def test_a_capped_list_keeps_the_worst_first_then_the_key(rows: list[tuple[str, str]]) -> None:
    capped = m.Capped(cap=10)
    for status, key in rows:
        capped.add(status, (key,), {"status": status, "key": key})
    expect = sorted(rows, key=lambda r: (ADR_SEVERITY.index(r[0]), r[1]))[:10]
    assert [(r["status"], r["key"]) for r in capped.rows()] == expect
    assert capped.total == len(rows)
    rec = capped.record({"name": "units.jsonl", "sha256": "ab"})
    assert rec["more"] == max(0, len(rows) - 10)
    assert (rec["more_in"] is None) == (len(rows) <= 10)


def test_every_enum_value_has_a_row_zeros_included() -> None:
    rows = m.zero_rows((s.value for s in ReconStatus), {"gap": 2})
    assert [r["value"] for r in rows] == [s.value for s in ReconStatus]
    assert {r["value"]: r["count"] for r in rows}["gap"] == 2
    assert all(r["count"] == 0 for r in rows if r["value"] != "gap")


def test_day_labels_come_from_the_unit_key_only() -> None:
    assert m.day_label("T1/C1/2026-03-08") == "2026-03-08"
    assert m.day_label("directory") is None
    assert m.day_label("C1/not-a-day") is None


def _inputs(**kw: object) -> m.ReportInputs:
    chain = m.ChainFold()
    chain.add(m.ChainEvent(1, "job_started", "user:alice", T0,
                           {"connector": "dummy", "connector_version": "0.4.0", "scopes": []}))  # fmt: skip
    chain.add(m.ChainEvent(2, "unit_reconciled", "system", T0, _reconciled()))
    chain.add(m.ChainEvent(3, "job_finished", "system", T0, {"status": "completed"}))
    base: dict[str, object] = {
        "job": {"id": "j", "status": "completed"}, "chain": chain,
        "verification": {"ok": True, "errors": []},
        "snapshot": {"renders": [], "retention_gaps": [], "digest": "d"}, "access": {},
        "unit_status_counts": {"done": 1}, "recon_counts": {"matched": 1},
        "totals": {"expected": 3, "collected": 3, "file_gaps": 0}, "exceptions": {},
        "observation_counts": {}, "pauses_db": [], "divergences": [],
        "normalizer_versions": ["0.2.0"], "versions": {}, "audit_events": [],
        "files": [m.JsonlDigest("units.jsonl")], "evidence": {}, "identity": {},
    }  # fmt: skip
    base.update(kw)
    return m.ReportInputs(**base)  # type: ignore[arg-type]


def test_report_json_is_canonical_states_unknowns_and_has_no_generation_facts() -> None:
    body = m.report_json(_inputs())
    doc = json.loads(body)
    assert body == m.report_json(_inputs())  # deterministic
    assert doc["job"]["clean"] is True and doc["banner"] == [
        "Complete: every unit reconciled against the source"
    ]
    assert doc["access"]["plan_tier"] == m.UNKNOWN and doc["access"]["blind_spots"] == m.UNKNOWN
    assert doc["job"]["unit_day_zone"] == m.ZONE_NOT_RECORDED
    assert doc["job"]["requested_by"] == "user:alice"
    statuses = [r["value"] for r in doc["counts"]["units_by_status"]]
    assert statuses == [s.value for s in UnitStatus]
    text = body.decode()
    for forbidden in ("report_id", "generated_at", "hostname"):
        assert forbidden not in text
    assert all("source" in section for section in doc.values() if isinstance(section, dict))


def test_a_divergence_makes_the_report_not_clean_and_says_so_first() -> None:
    doc = m.report_document(_inputs(divergences=[m.Divergence("unit_differs", "u", 1, 2)]))
    assert doc["job"]["clean"] is False
    assert doc["banner"][0].startswith("RECORDS DISAGREE")


@given(
    status=st.sampled_from([s.value for s in JobStatus]),
    custody_ok=st.booleans(),
    divergences=st.integers(0, 2),
    gaps=st.integers(0, 2),
    recon=st.dictionaries(st.sampled_from([s.value for s in ReconStatus]), st.integers(0, 5)),
)
def test_nothing_looks_clean_that_is_not(
    status: str, custody_ok: bool, divergences: int, gaps: int, recon: dict[str, int]
) -> None:
    """ADR 0018 §4 / §15: the clean words appear iff the clean function says so; archive status
    always carries the caveat byte for byte; custody and record problems lead; every enum row."""
    chain = m.ChainFold()
    chain.add(m.ChainEvent(1, "job_started", "u", T0, {"scopes": []}))
    chain.add(m.ChainEvent(2, "job_finished", "system", T0, {"status": status}))
    doc = m.report_document(_inputs(
        chain=chain, verification={"ok": custody_ok, "errors": [] if custody_ok else ["x"]},
        divergences=[m.Divergence("unit_differs", f"u{i}", 1, 2) for i in range(divergences)],
        snapshot={"renders": [], "retention_gaps": [{}] * gaps, "digest": "d"},
        recon_counts=recon,
    ))  # fmt: skip
    clean = m.clean(status, custody_ok=custody_ok, divergences=divergences, retention_gaps=gaps)
    words = "Complete: every unit reconciled against the source"
    assert doc["job"]["clean"] is clean
    assert (words in doc["banner"]) is clean
    if status == "completed_against_archive":
        assert ARCHIVE_CAVEAT in doc["banner"] and doc["job"]["archive_caveat"] == ARCHIVE_CAVEAT
    if not custody_ok:
        assert doc["banner"][0] == "CUSTODY VERIFICATION FAILED"
    elif divergences:
        assert doc["banner"][0].startswith("RECORDS DISAGREE")
    values = [r["value"] for r in doc["counts"]["units_by_recon_status"]]
    assert values[: len(ReconStatus)] == [s.value for s in ReconStatus]
