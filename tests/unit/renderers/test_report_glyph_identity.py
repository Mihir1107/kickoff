"""The glyph set is part of the report's identity (ADR 0018 §24): `report.html` replaces every
character no vendored font draws, so a font change that changes the coverage changes the HTML's
bytes; the identity (and its unique index), the report queue and the golden key must change too,
also where the PDF toolchain id is `none`."""

from __future__ import annotations

import pytest

from edisc_renderers.report import html as rhtml
from edisc_worker.contracts import report_task_queue
from edisc_worker.reports import runtime_identity

from .test_report_golden import CASES, build, key


def _snapshot() -> tuple[str, str, str, bytes]:
    ident = runtime_identity()
    return (
        ident["glyph_set"],
        report_task_queue(**ident),
        key(),
        build("at_the_cap")["report.html"],
    )


def test_a_font_change_makes_a_new_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot()
    assert before[0] == rhtml.glyph_set_id() and len(before[0]) == 64
    assert before[1].endswith(f".g{before[0][:12]}") and f"_glyphs-{before[0][:12]}" in before[2]
    starts, ends = rhtml._coverage()
    keep = [(lo, hi) for lo, hi in zip(starts, ends, strict=True) if hi < 0x0E01 or lo > 0x0E7F]
    rhtml.glyph_set_id.cache_clear()
    monkeypatch.setattr(rhtml, "_coverage", lambda: ([a for a, _ in keep], [b for _, b in keep]))
    try:
        after = _snapshot()
    finally:
        rhtml.glyph_set_id.cache_clear()
    assert after[0] != before[0]  # identity (and so the one-live-report index)
    assert after[1] != before[1]  # queue: a worker of the old font set never builds it
    assert after[2] != before[2]  # golden key
    assert after[3] != before[3]  # because the HTML's bytes did change (Thai now markers)
    assert "at_the_cap" in CASES
