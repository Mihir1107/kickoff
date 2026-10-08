"""The collection report HTML builder and its sanitiser (`edisc_renderers.report.html`, ADR 0018
§3.2, §4). Expected structure is checked by parsing the HTML (stdlib `html.parser`); the sanitiser is
fuzzed and exercised on every hard-string class from spike S1 (§5.10)."""

from __future__ import annotations

import base64
import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edisc_renderers.report import html as rhtml
from edisc_renderers.report.model import UNKNOWN
from edisc_renderers.report.version import REPORT_RENDERER_VERSION

# every hard case spike S1 names (names and error texts), §5.10
HARD = {
    "cjk_sc": "张伟（销售部）",  # noqa: RUF001  (fullwidth parens are the test datum)
    "japanese": "山田太郎・営業",
    "korean": "김민준 팀장",
    "arabic": "محمد عبد الله",
    "hebrew": "שרה כהן",
    "mixed_bidi": "Report: تقرير 2026 — שלב 3",
    "emoji_skin_flag": "🎉 launch 👍🏽 🇮🇳",
    "zwj_family": "👩‍👩‍👧‍👦 family",
    "combining_zalgo": "é̂ Zalgo z̶a̷l̸",
    "devanagari": "नमस्ते क्षत्रिय",
    "thai": "สวัสดีครับ",
    "zero_width": "zero​width‌joiner﻿bom",
    "bidi_override": "‮evil.exe‬ override",
    "uncovered": "Linear B \U00010000 and \U00013000",
    "plain_ascii": "plain ASCII channel #general",
    "html_injection": "<script>alert(1)</script> & \"'",
}

ALLOWED_TAGS = {
    "html", "head", "meta", "title", "style", "body", "h1", "h2",
    "div", "span", "p", "table", "tr", "th", "td", "bdi", "br",
}  # fmt: skip


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: set[str] = set()
        self.attrs: list[tuple[str, str | None]] = []
        self.styles: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.attrs += attrs
        self._in_style = tag == "style"

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.styles.append(data)


def parse(html_bytes: bytes) -> _Collector:
    c = _Collector()
    c.feed(html_bytes.decode("utf-8"))
    return c


def _sec(rows: Sequence[Mapping[str, Any]], *, total: int | None = None, more: int = 0,
         more_in: Mapping[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
    return {"rows": list(rows), "total": total if total is not None else len(rows),
            "more": more, "more_in": more_in}  # fmt: skip


def doc(*, clean: bool = True, status: str = "completed", banner: Sequence[str] = (),
        unit_rows: Sequence[Mapping[str, Any]] = (), units_total: int | None = None,
        units_more: int = 0, obs_rows: Sequence[Mapping[str, Any]] = (),
        access: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "banner": list(banner)
        or (
            ["Complete: every unit reconciled against the source"]
            if clean
            else ["NOT COMPLETE: status " + status]
        ),
        "job": {
            "source": "job_chain",
            "id": "j1",
            "status": status,
            "clean": clean,
            "unit_day_zone": "UTC",
        },
        "access": access
        or {
            "source": "job_chain",
            "connector": "slack",
            "plan_tier": "pro",
            "blind_spots": None,
            "database": {},
            "export": None,
        },
        "scopes": {"source": "job_chain", "scopes": []},
        "exceptions": {
            "source": "job_chain",
            "units": _sec(
                unit_rows,
                total=units_total,
                more=units_more,
                more_in={"name": "units.jsonl", "sha256": "a" * 64} if units_more else None,
            ),
            "observations_capped": _sec(obs_rows),
            "observations": [{"value": "file_unavailable", "count": len(obs_rows)}],
        },
        "counts": {
            "source": "job_chain",
            "units_by_status": [{"value": "done", "count": 3}, {"value": "failed", "count": 0}],
            "units_by_recon_status": [
                {"value": "matched", "count": 3},
                {"value": "gap", "count": 0},
            ],
        },
        "pauses": {"source": "job_chain", "never_resumed": 0, "pauses": []},
        "versions": {
            "source": "job_chain",
            "report_renderer": REPORT_RENDERER_VERSION,
            "pdf_toolchain": None,
        },
        "custody_verification": {"source": "job_chain", "ok": True, "events": 3},
        "evidence_store": {
            "source": "snapshot",
            "not_complete": 0,
            "retention_gaps": [],
            "lock": {},
        },
        "renders": {"source": "snapshot", "renders": 0, "file": None},
        "divergences": {"source": "job_chain", "divergences": []},
        "integrity": {
            "source": "snapshot",
            "snapshot_digest": "d",
            "identity": {"paper": "letter"},
            "files": [{"name": "units.jsonl", "rows": 1, "size": 10, "sha256": "b" * 64}],
        },
    }


def test_only_allowed_elements_no_handlers_no_links_one_style() -> None:
    out = rhtml.report_html(doc())
    c = parse(out)
    assert c.tags <= ALLOWED_TAGS, c.tags - ALLOWED_TAGS
    assert not any(k.startswith("on") for k, _ in c.attrs), "no event-handler attributes"
    assert not any(k in ("href", "src") for k, _ in c.attrs), (
        "report HTML has no links or resources"
    )
    assert len(c.styles) == 1
    digest = base64.b64encode(hashlib.sha256(c.styles[0].encode()).digest()).decode()
    assert f"sha256-{digest}" == rhtml.STYLE_CSP_HASH
    assert rhtml.STYLE_CSP_HASH in out.decode()  # the CSP meta carries the live style's hash


def test_the_clean_banner_and_the_not_clean_banner() -> None:
    assert b'class="banner clean"' in rhtml.report_html(doc(clean=True))
    assert b'class="banner notclean"' in rhtml.report_html(doc(clean=False, status="failed"))


def test_a_capped_list_names_the_remainder_by_file_and_sha256() -> None:
    rows = [
        {
            "recon_status": "gap",
            "unit_key": "c/2026-01-02",
            "expected": 9,
            "collected": 7,
            "error": None,
        }
    ]
    out = rhtml.report_html(doc(unit_rows=rows, units_total=1205, units_more=1204)).decode()
    assert "1,205 total" in out and "1,204 more in units.jsonl" in out and "a" * 64 in out


def test_every_enum_value_has_a_row_and_unknown_is_shown() -> None:
    out = rhtml.report_html(
        doc(
            access={
                "source": "not_recorded",
                "connector": UNKNOWN,
                "plan_tier": UNKNOWN,
                "blind_spots": UNKNOWN,
                "database": {},
                "export": None,
            }
        )
    ).decode()
    assert UNKNOWN in out
    assert "<bdi>failed</bdi></td><td>0</td>" in out  # a zero row is present, not hidden


def test_deterministic_bytes() -> None:
    assert rhtml.report_html(doc()) == rhtml.report_html(doc())


def test_bidi_controls_are_replaced_and_invisibles_are_marked() -> None:
    assert "‮" not in rhtml.reveal("‮evil")  # replaced, not kept
    assert rhtml.marker(0x202E) in rhtml.reveal("‮evil")
    assert "​" + rhtml.marker(0x200B) in rhtml.reveal("a​b")  # kept, marker after
    assert rhtml.reveal("plain") == "plain"


def test_escaping_covers_the_five_characters() -> None:
    assert rhtml.user("<a>&\"'") == "<bdi>&lt;a&gt;&amp;&quot;&#x27;</bdi>"


@pytest.mark.parametrize("name", list(HARD))
def test_a_report_with_each_hard_string_class_is_safe(name: str) -> None:
    s = HARD[name]
    rows = [
        {
            "recon_status": "failed",
            "unit_key": f"{s}/2026-01-02",
            "expected": 1,
            "collected": 0,
            "error": s,
        }
    ]
    out = rhtml.report_html(
        doc(
            clean=False,
            status="failed",
            unit_rows=rows,
            obs_rows=[
                {
                    "kind": "file_unavailable",
                    "unit_key": s,
                    "item_id": "i",
                    "file_id": "f",
                    "reason": s,
                }
            ],
        )
    )
    c = parse(out)  # still well-formed
    assert c.tags <= ALLOWED_TAGS
    text = out.decode()
    for cp in rhtml.BIDI_CONTROLS:
        assert chr(cp) not in text  # no bidi control survives anywhere in the page
    assert "<script>" not in text  # injection escaped
    for ch in s:  # every invisible/format/control character is annotated
        if ord(ch) in rhtml.BIDI_CONTROLS or unicodedata.category(ch) in ("Cf", "Cc", "Zl", "Zp"):
            assert rhtml.marker(ord(ch)) in text, (name, hex(ord(ch)))


@given(st.text(max_size=200))
@settings(max_examples=300)
def test_user_sanitiser_fuzz(text: str) -> None:
    cell = rhtml.user(text)
    # no raw bidi control, and no unescaped markup, ever reaches the output
    for cp in rhtml.BIDI_CONTROLS:
        assert chr(cp) not in cell
    inner = cell[len("<bdi>") : -len("</bdi>")]
    assert "<" not in inner and ">" not in inner  # all escaped (markers use [U+..], not <>)
    # every reveal-category character is annotated, every bidi control replaced by its marker
    for ch in text:
        cp = ord(ch)
        if cp in rhtml.BIDI_CONTROLS:
            assert chr(cp) not in cell and rhtml.marker(cp) in cell
        elif unicodedata.category(ch) in ("Cf", "Cc", "Zl", "Zp"):
            assert rhtml.marker(cp) in cell


@given(st.text(max_size=120))
@settings(max_examples=120)
def test_the_whole_page_stays_well_formed_for_any_user_string(text: str) -> None:
    rows = [
        {
            "recon_status": "failed",
            "unit_key": f"{text}/2026-01-02",
            "expected": 1,
            "collected": 0,
            "error": text,
        }
    ]
    out = rhtml.report_html(doc(clean=False, status="failed", unit_rows=rows))
    c = parse(out)  # parses without raising
    assert c.tags <= ALLOWED_TAGS
    assert not any(k.startswith("on") for k, _ in c.attrs)
