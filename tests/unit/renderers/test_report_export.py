"""The export a job collected from, in the collection report (ADR 0018 §32): what `job_started.export`
records (declared plan, detected tier, blind spots, tier warnings, validation findings) is what the
report states, from the chain; a job recorded before §32 prints UNKNOWN for what it lacks; the
HTML states the blind spots of the export (what it cannot contain) and every finding."""

from __future__ import annotations

from typing import Any

from edisc_renderers.report import html as rhtml
from edisc_renderers.report import model as m
from edisc_renderers.report.recompute import _access

from .test_report_golden import EXPORT, build
from .test_report_html import _NS, doc, parse, plain, strict_tree


def _access_with(export: Any) -> dict[str, Any]:
    return {"source": "job_chain", "connector": "slack_export", "plan_tier": "public_only",
            "blind_spots": EXPORT["blind_spots"], "database": {}, "export": export}  # fmt: skip


def test_the_chain_record_is_stated_field_by_field() -> None:
    assert m.export_section(EXPORT) == {k: EXPORT[k] for k in m.EXPORT_FIELDS}
    assert m.export_section(None) is None  # not an export job: no export section


def test_a_job_recorded_before_section_32_prints_unknown_for_what_it_lacks() -> None:
    old = {"id": "e1", "sha256": "ab" * 32}
    stated = m.export_section(old)
    assert stated is not None
    assert (stated["id"], stated["sha256"]) == ("e1", "ab" * 32)
    for k in set(m.EXPORT_FIELDS) - {"id", "sha256"}:
        assert stated[k] == m.UNKNOWN, k
    # an undeclared plan is recorded as null and stays null: it is not unknown
    assert m.export_section({**EXPORT, "declared_plan": None})["declared_plan"] is None  # type: ignore[index]


def test_the_verifier_recomputes_the_same_export_section() -> None:
    assert _access({"export": EXPORT})["export"] == m.export_section(EXPORT)
    assert _access({})["export"] is None


def test_the_html_states_what_a_free_plan_export_cannot_contain() -> None:
    page = build("archive")["report.html"]
    parse(page)  # both parsers, strict html5lib
    text = plain(strict_tree(page))
    for spot in EXPORT["blind_spots"]:
        assert spot in text
    assert "Private channels, direct messages and group direct messages are not in this" in text
    assert EXPORT["tier_warnings"][0] in text
    for needle in ("declared plan", "free", "detected tier", "public_only", "1,234,567",
                   "entries_by_kind.day_file", "unknown_entries.sample"):  # fmt: skip
        assert needle in text, needle


def test_a_hostile_entry_name_in_the_findings_is_revealed_not_obeyed() -> None:
    page = build("archive")["report.html"]
    tree = strict_tree(page)
    cells = [c for c in tree.iter(f"{_NS}td") if "evil.exe" in plain(c)]
    assert cells
    assert chr(0x202E) not in page.decode("utf-8")  # the override is replaced by its marker


def test_an_old_export_job_prints_unknown_in_the_html() -> None:
    page = rhtml.report_html(
        doc(access=_access_with(m.export_section({"id": "e1", "sha256": "0" * 64})))
    )
    parse(page)
    text = plain(strict_tree(page))
    assert text.count(m.UNKNOWN) >= 6  # size, entries, tier, confirmed, spots, warnings, findings


def test_no_export_section_for_a_live_job() -> None:
    page = rhtml.report_html(doc())
    assert b"<h2>Export" not in page
    assert b"<h2>Export" in rhtml.report_html(doc(access=_access_with(m.export_section(EXPORT))))
