"""The collection report HTML builder and its sanitiser (`edisc_renderers.report.html`, ADR 0018
§3.2, §4). Expected structure is checked by parsing the HTML twice: the stdlib `html.parser` and a
STRICT HTML5 parser (html5lib with `strict=True`: any parse error, including an invalid code point,
raises). The sanitiser is fuzzed over every character class it treats specially (bidi controls, C0,
C1, NUL, noncharacters, lone surrogates, other format characters) and exercised on every hard-string
class from spike S1 (§5.10)."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import Any

import html5lib
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edisc_renderers.report import html as rhtml
from edisc_renderers.report.model import PAUSE_ENDS, UNKNOWN
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


_NS = "{http://www.w3.org/1999/xhtml}"


def strict_tree(html_bytes: bytes) -> ET.Element:
    """html5lib in strict mode: raises on ANY HTML5 parse error (invalid code points included)."""
    tree: ET.Element = html5lib.HTMLParser(strict=True, namespaceHTMLElements=True).parse(
        html_bytes.decode("utf-8")
    )
    return tree


def parse(html_bytes: bytes) -> _Collector:
    """Both parsers; the strict one must accept the page and agree on the elements used."""
    tree = strict_tree(html_bytes)
    c = _Collector()
    c.feed(html_bytes.decode("utf-8"))
    strict_tags = {e.tag.removeprefix(_NS) for e in tree.iter()} - {"tbody"}  # implied by HTML5
    assert strict_tags == c.tags, strict_tags ^ c.tags
    return c


def markers(node: ET.Element) -> list[str]:
    """The code-point marker ELEMENTS under ``node``."""
    return [e.text or "" for e in node.iter(f"{_NS}span") if e.get("class") == "cp"]


def plain(node: ET.Element) -> str:
    """The user text under ``node`` with every marker element removed (what is left of the input)."""
    out = [node.text or ""] if not (node.tag == f"{_NS}span" and node.get("class") == "cp") else []
    for child in node:
        out.append(plain(child))
        out.append(child.tail or "")
    return "".join(out)


def cell(text: str) -> ET.Element:
    """``user(text)`` parsed strictly inside a minimal page; returns its `<bdi>`."""
    page = f"<!doctype html><html><head><title>t</title></head><body><p>{rhtml.user(text)}</p></body></html>"
    bdi = strict_tree(page.encode("utf-8")).find(f".//{_NS}bdi")
    assert bdi is not None
    return bdi


def _sec(rows: Sequence[Mapping[str, Any]], *, total: int | None = None, more: int = 0,
         more_in: Mapping[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
    return {"rows": list(rows), "total": total if total is not None else len(rows),
            "more": more, "more_in": more_in}  # fmt: skip


def doc(*, clean: bool = True, status: str = "completed", banner: Sequence[str] = (),
        unit_rows: Sequence[Mapping[str, Any]] = (), units_total: int | None = None,
        units_more: int = 0, obs_rows: Sequence[Mapping[str, Any]] = (),
        access: Mapping[str, Any] | None = None,
        pauses: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:  # fmt: skip
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
        "pauses": {
            "source": "job_chain",
            "pauses": list(pauses),
            "by_end": [
                {"value": v, "count": sum(1 for p in pauses if p["ended_how"] == v)}
                for v in PAUSE_ENDS
            ],
        },
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


def _pause(how: str, *, by: str | None = "user:dana", reason: str | None = None) -> dict[str, Any]:
    ended = how != "not_ended"
    return {"reason": "invalid_auth (credentials_revoked)", "reason_class": "credentials_revoked",
            "connection_id": "c-1", "paused_at": "2026-10-01T10:00:00Z",
            "deadline_at": "2026-10-08T10:00:00+00:00", "ended_how": how,
            "ended_at": "2026-10-02T10:00:00Z" if ended else None, "ended_by": by if ended else None,
            "end_reason": reason, "duration_ms": 86_400_000 if ended else None}  # fmt: skip


def _pause_rows(out: bytes) -> list[list[str]]:
    """The pause table's rows (cell text, markers removed), from the strict parse."""
    tree = strict_tree(out)
    heading = next(h for h in tree.iter(f"{_NS}h2") if (h.text or "").startswith("Pauses"))
    body = tree.find(f"{_NS}body")
    assert body is not None
    children = list(body)
    table = children[children.index(heading) + 1]
    assert table.tag == f"{_NS}table", "the pause table follows its heading"
    return [[plain(c).strip() for c in tr] for tr in table.iter(f"{_NS}tr")][1:]


def test_every_pause_states_how_it_ended_who_when_and_why() -> None:
    """ADR 0016 A6: re-authorized (who), the deadline, finished without access (who, the reason),
    and a pause the chain never ends, each a row; the counts by ending list every value."""
    out = rhtml.report_html(doc(clean=False, status="completed_with_gaps", pauses=[
        _pause("reauthorized", by="user:dana"),
        _pause("deadline", by="system:access-lost"),
        _pause("finished_without_access", by="user:sam", reason="custodian left; access gone"),
        _pause("not_ended"),
    ]))  # fmt: skip
    rows = _pause_rows(out)
    cols = [h for h, _ in rhtml.PAUSE_COLUMNS]
    by = {r[cols.index("ended")]: dict(zip(cols, r, strict=True)) for r in rows}
    assert set(by) == {"reauthorized", "deadline", "finished_without_access", "not_ended"}
    assert by["reauthorized"]["ended by"] == "user:dana"
    assert by["deadline"]["ended by"] == "system:access-lost"
    assert by["finished_without_access"]["ended by"] == "user:sam"
    assert by["finished_without_access"]["reason given"] == "custodian left; access gone"
    assert by["deadline"]["deadline"] == "2026-10-08T10:00:00+00:00"
    assert by["not_ended"]["ended at"] == "none" and by["not_ended"]["ended by"] == "none"
    assert all(r["reason class"] == "credentials_revoked" for r in by.values())
    text = out.decode()
    assert "Pauses by how they ended:" in text
    for value in PAUSE_ENDS:
        assert f"<td><bdi>{value}</bdi></td><td>1</td>" in text, value


def test_no_pause_says_none() -> None:
    text = rhtml.report_html(doc()).decode()
    assert "</h2><p>None.</p><p>Pauses by how they ended:</p>" in text


def test_deterministic_bytes() -> None:
    assert rhtml.report_html(doc()) == rhtml.report_html(doc())


def test_bidi_controls_are_replaced_and_invisibles_are_marked() -> None:
    assert "\u202e" not in rhtml.reveal("\u202eevil")  # replaced, not kept
    assert rhtml.marker(0x202E) in rhtml.reveal("\u202eevil")
    assert "\u200b" + rhtml.marker(0x200B) in rhtml.reveal("a\u200bb")  # kept, marker after
    assert rhtml.reveal("plain") == "plain"


def test_escaping_covers_the_five_characters() -> None:
    assert rhtml.user("<a>&\"'") == "<bdi>&lt;a&gt;&amp;&quot;&#x27;</bdi>"


def test_a_marker_is_an_element_and_literal_marker_text_is_not() -> None:
    """Fix 1: the literal text "[U+202E]" and the real U+202E must render differently. The real
    character becomes a styled marker ELEMENT; the literal text stays escaped text with no marker."""
    literal, real = "[U+202E]", "\u202e"
    assert rhtml.user(literal) != rhtml.user(real)
    lit, rl = cell(literal), cell(real)
    assert markers(lit) == [] and plain(lit) == literal  # text, exactly as written
    assert markers(rl) == ["U+202E"] and plain(rl) == ""  # one marker element, the control dropped
    assert "[U+202E]" not in rhtml.user(real)
    # a user string cannot forge the element: its markup is escaped
    forged = '<span class="cp">U+202E</span>'
    # wherever it sits: at the end, before a REPLACED character, before a KEPT one
    for tail, want in (("", []), ("\u202e", ["U+202E"]), ("\u200b", ["U+200B"])):
        c = cell(forged + tail)
        assert markers(c) == want and plain(c) == forged + tail.replace("\u202e", "")
    # and the marker is visibly a marker on screen and in print: its own boxed style
    assert ".cp{" in rhtml.STYLE and "background:" in rhtml.STYLE.split(".cp{", 1)[1].split("}")[0]


# fix 2: every class of code point an HTML document must not contain -> replaced by its marker
INVALID = {
    "nul": "\x00", "c0_soh": "\x01", "c0_vt": "\x0b", "c0_ff": "\x0c", "c0_us": "\x1f",
    "del": "\x7f", "c1_nel": "\x85", "c1_apc": "\x9f", "nonchar_fdd0": "\ufdd0",
    "nonchar_fdef": "\ufdef", "nonchar_fffe": "\ufffe", "nonchar_ffff": "\uffff",
    "nonchar_plane1": "\U0001fffe", "nonchar_plane16": "\U0010ffff",
    "lone_high_surrogate": json.loads('"\\ud800"'), "lone_low_surrogate": json.loads('"\\udfff"'),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(INVALID))
def test_code_points_invalid_in_html_are_replaced_not_kept(name: str) -> None:
    ch = INVALID[name]
    text = f"a{ch}b"
    assert rhtml.invalid_in_html(ord(ch))
    out = rhtml.report_html(doc(clean=False, status="failed", unit_rows=[_failed_row(text)]))
    page = out.decode("utf-8")  # encodable (a lone surrogate would raise here if it were kept)
    assert ch not in page
    parse(out)  # strict html5lib: no invalid-codepoint error
    c = cell(text)
    assert markers(c) == [f"U+{ord(ch):04X}"] and plain(c) == "ab"


def test_tab_lf_cr_stay_with_a_marker_after_them() -> None:
    for ch in ("\t", "\n", "\r"):
        assert not rhtml.invalid_in_html(ord(ch))
        assert rhtml.reveal(f"a{ch}b") == f"a{ch}{rhtml.marker(ord(ch))}b"


def test_lone_surrogates_from_escaped_json_neither_crash_nor_corrupt() -> None:
    """A `str` decoded from escaped JSON can hold a lone surrogate. The page still builds, encodes
    and parses; the surrogate is shown as its marker; a VALID pair in the same text stays the
    character it encodes; nothing else changes."""
    text = json.loads('"x\\ud83d\\ude00y\\udc00z\\ud800"')
    assert text == "x\U0001f600y\udc00z\ud800"
    c = cell(text)
    assert markers(c) == ["U+DC00", "U+D800"]
    assert plain(c) == "x\U0001f600yz"
    out = rhtml.report_html(doc(clean=False, status="failed", unit_rows=[_failed_row(text)]))
    parse(out)


def test_report_json_refuses_a_lone_surrogate_loudly() -> None:
    """`report.json` keeps user strings EXACTLY; RFC 8785 cannot represent a lone surrogate, so the
    canonical encoder refuses (a failed report, never silently altered data)."""
    from edisc_core.canonical import CanonicalizationError, canonical_json

    with pytest.raises(CanonicalizationError):
        canonical_json({"error": json.loads('"\\ud800"')})


def _failed_row(text: str) -> dict[str, Any]:
    return {"recon_status": "failed", "unit_key": f"{text}/2026-01-02", "expected": 1,
            "collected": 0, "error": text}  # fmt: skip


@pytest.mark.parametrize("name", list(HARD))
def test_a_report_with_each_hard_string_class_is_safe(name: str) -> None:
    s = HARD[name]
    out = rhtml.report_html(
        doc(
            clean=False,
            status="failed",
            unit_rows=[_failed_row(s)],
            obs_rows=[
                {
                    "kind": "file_unavailable",
                    "unit_key": s,
                    "item_id": "i",
                    "file_id": "f",
                    "reason": s,
                }
            ],
            pauses=[_pause("finished_without_access", by=s, reason=s)],
        )
    )
    c = parse(out)  # well-formed for both parsers
    assert c.tags <= ALLOWED_TAGS
    text = out.decode()
    for cp in rhtml.BIDI_CONTROLS:
        assert chr(cp) not in text  # no bidi control survives anywhere in the page
    assert "<script>" not in text  # injection escaped
    for ch in s:  # every invisible/format/control character is annotated
        if rhtml.replaced(ord(ch)) or unicodedata.category(ch) in ("Cf", "Cc", "Zl", "Zp"):
            assert rhtml.marker(ord(ch)) in text, (name, hex(ord(ch)))


# every class the sanitiser treats specially, plus arbitrary text: hypothesis's default alphabet
# never draws surrogates and rarely the C0/C1/noncharacter classes, so they are drawn explicitly
SPECIAL = st.one_of(
    st.sampled_from(sorted(rhtml.BIDI_CONTROLS)),
    st.integers(0x00, 0x1F),  # NUL and C0 (tab, LF, CR are kept with a marker)
    st.integers(0x7F, 0x9F),  # DEL and C1
    st.integers(0xD800, 0xDFFF),  # lone surrogates
    st.integers(0xFDD0, 0xFDEF),
    st.integers(0, 16).flatmap(lambda p: st.sampled_from([p << 16 | 0xFFFE, p << 16 | 0xFFFF])),
    st.sampled_from([0x200B, 0x200C, 0x200D, 0xFEFF, 0x2028, 0x2029, 0x00AD, 0xE0001]),
).map(chr)
FUZZ_TEXT = st.lists(st.one_of(st.characters(), SPECIAL), max_size=60).map("".join)


def _expected_plain(text: str) -> str:
    """What must survive of ``text``: everything but the replaced characters, unchanged (HTML
    parsing itself turns a CR into LF; a CR is always followed by its marker element, so the page
    never holds a CRLF pair that parsing would merge: "\\r\\n" is two line breaks, each marked)."""
    kept = "".join(ch for ch in text if not rhtml.replaced(ord(ch)))
    return kept.replace("\r", "\n")


@given(FUZZ_TEXT)
@settings(max_examples=400)
def test_user_sanitiser_fuzz(text: str) -> None:
    out = rhtml.user(text)
    out.encode("utf-8")  # never a lone surrogate in the output
    for ch in out:
        cp = ord(ch)
        assert cp not in rhtml.BIDI_CONTROLS and not rhtml.invalid_in_html(cp), hex(cp)
    c = cell(text)  # strict html5lib accepts it
    want = [f"U+{ord(ch):04X}" for ch in text
            if rhtml.replaced(ord(ch)) or unicodedata.category(ch) in ("Cf", "Cc", "Zl", "Zp")]  # fmt: skip
    assert markers(c) == want  # every replaced or invisible character has its marker, in order
    assert plain(c) == _expected_plain(text)  # nothing else lost or altered
    assert [e.tag.removeprefix(_NS) for e in c.iter()] == ["bdi"] + ["span"] * len(want)


@given(FUZZ_TEXT)
@settings(max_examples=150)
def test_the_whole_page_stays_well_formed_for_any_user_string(text: str) -> None:
    out = rhtml.report_html(
        doc(clean=False, status=text or "failed", unit_rows=[_failed_row(text)])
    )
    c = parse(out)  # both parsers, the strict one raising on any parse error
    assert c.tags <= ALLOWED_TAGS
    assert not any(k.startswith("on") for k, _ in c.attrs)
    assert all(k == "class" or k in ("lang", "charset", "content", "http-equiv", "name")
               for k, _ in c.attrs)  # fmt: skip


@pytest.mark.parametrize("total", [1_000, 1_001])
def test_the_conversation_list_names_conversations_jsonl_only_above_the_cap(total: int) -> None:
    rows = [{"worst_status": "matched", "conversation_id": f"C{i:05d}", "units": 1, "expected": 3,
             "collected": 3, "file_gaps": 0} for i in range(min(total, 1_000))]  # fmt: skip
    more = total - len(rows)
    d = doc()
    d["conversations"] = {"source": "job_chain", "rows": rows, "total": total, "more": more,
                          "more_in": {"name": "conversations.jsonl", "sha256": "c" * 64}
                          if more else None}  # fmt: skip
    out = rhtml.report_html(d)
    parse(out)
    text = out.decode()
    assert f"{total:,} total; showing the 1,000 worst" in text
    assert text.count("<bdi>C0") == 1_000
    assert ("1 more in conversations.jsonl, SHA-256 " + "c" * 64 in text) is (total > 1_000)


def test_characters_no_vendored_font_draws_are_replaced_by_their_marker() -> None:
    """§5.5: the PDF renders the stored HTML, so the HTML already shows a marker wherever no
    vendored font has a glyph (never tofu, never a silent substitute)."""
    c = cell("Linear B \U00010000 and \U00013000")
    assert markers(c) == ["U+10000", "U+13000"] and plain(c) == "Linear B  and "
    for s in ("张伟", "김민준", "محمد", "שרה", "नमस्ते", "สวัสดี", "🎉👍🏽", "é̂"):
        assert markers(cell(s)) == [] and plain(cell(s)) == s, s  # covered scripts stay as they are
    assert rhtml.covered(0x41) and not rhtml.covered(0x10000)
    assert not rhtml.replaced(0x3000)  # whitespace is never replaced


def test_the_coverage_file_is_tied_to_fonts_lock() -> None:
    """Changing a font in fonts.lock without regenerating coverage.txt fails here, everywhere (the
    report image recomputes the coverage from the installed fonts too)."""
    from scripts.report_font_coverage import COVERAGE, lock_fonts

    header = [line.split()[2:] for line in COVERAGE.read_text().splitlines()
              if line.startswith("# font ")]  # fmt: skip
    assert [tuple(h) for h in header] == lock_fonts()
    assert len(lock_fonts()) == 10  # the vendored set of ADR 0018 §5.4 (no Japanese font)


def test_the_page_carries_the_seal_dates_and_its_identity_for_the_pdf() -> None:
    d = doc()
    d["job"]["sealed_at"] = "2026-01-06T12:34:56Z"
    out = rhtml.report_html(d).decode()
    assert '<meta content="2026-01-06T12:34:56Z" name="dcterms.created">' in out
    assert '<meta content="2026-01-06T12:34:56Z" name="dcterms.modified">' in out
    assert '<p class="pageid src">job <bdi>j1</bdi> · snapshot <bdi>d</bdi></p>' in out
    assert out.index('class="pageid') < out.index("<h2>")  # before content: footer from page 1


# ------------------------------------------------------------------ lone surrogates (ADR 0018 §23)
def test_lone_surrogates_are_valid_in_report_json_counted_and_marked_in_html() -> None:
    from edisc_core.lone_surrogates import has_lone, restore
    from edisc_renderers.report import model as m
    from tests.unit.renderers.test_report_golden import CASES

    exact = m.report_document(CASES["gaps_and_failures_hard_strings"]())
    doc = m.valid_document(exact)
    body = json.dumps(doc, ensure_ascii=False).encode("utf-8")  # encodable: nothing raw left
    lone = doc["integrity"]["lone_surrogates"]
    assert lone["report_json"] and lone["count"] == len(lone["report_json"]) + sum(
        f["lone_surrogates"] for f in doc["integrity"]["files"]
    )
    assert b"\\ud8" not in body and "�" in body.decode()
    without = {**doc, "integrity": {k: v for k, v in doc["integrity"].items()
                                    if k != "lone_surrogates"}}  # fmt: skip
    restored = restore(without, lone["report_json"])
    assert restored == {**exact, "integrity": restored["integrity"]}  # lossless
    assert not has_lone(m.valid_document(exact)["job"]["id"] or "")
    page = rhtml.report_html(doc)
    tree = strict_tree(page)  # strict html5lib: no invalid code point reached the page
    text = "".join(tree.itertext())
    for cp in (0xD83D, 0xDC00, 0xDE00):  # every surrogate of the hard string: a marker element
        assert f"U+{cp:04X}" in text
    assert b"Strings holding lone UTF-16 surrogates: " + str(lone["count"]).encode() in page


def test_a_unit_failed_error_recorded_with_its_code_units_is_stated_exactly() -> None:
    from edisc_renderers.report import model as m

    exact = "bad name " + chr(0xD800) + " here"
    units = [ord(c) for c in exact]
    payload = {"unit_key": "C1/2026-01-05", "error_type": "SourceError",
               "error": "bad name � here", "error_utf16": units}  # fmt: skip
    fact = m.unit_fact_from_event("unit_failed", payload)
    assert fact.error == "bad name � here" and fact.error_exact == exact
    # the database holds the valid text only: the facts still agree (no divergence)
    row = {"unit_key": "C1/2026-01-05", "status": "failed",
           "last_error": "SourceError: bad name � here"}  # fmt: skip
    assert fact == m.unit_fact_from_row(row, no_longer_observed=0, archive_backed=False)
    stated = m.unit_row({"unit_key": "C1/2026-01-05", "kind": "conversation_day",
                         "conversation_id": "C1"}, stated=fact, zone="UTC", scopes=[0],
                        divergent=False)  # fmt: skip
    assert stated["error"] == exact
    line = m.jsonl_line(stated)
    row_out = json.loads(line)
    assert row_out["error"] == "bad name � here"
    assert row_out["lone_surrogates"] == {"/error": units}
    digest = m.JsonlDigest("units.jsonl")  # each file's record counts its rows' strings
    assert digest.row(stated) == line and digest.row({"unit_key": "plain"}) != line
    assert digest.record()["lone_surrogates"] == 1 and digest.record()["rows"] == 2
    with pytest.raises(ValueError, match="code units"):
        m.unit_fact_from_event("unit_failed", {**payload, "error": "something else"})
