"""The report PDF, rendered by the real child in the report image (ADR 0018 §5, §6, §15)."""

from __future__ import annotations

import hashlib
import re

import pytest

from edisc_renderers.report import html as rhtml
from edisc_worker.report_pdf import ReportPdfRenderError, complete_pdf
from tests.unit.renderers.test_report_golden import build

from . import pdfcheck
from .conftest import render

SEALED = "20260106123456"  # the golden cases' sealed_at, 2026-01-06T12:34:56Z


@pytest.fixture(scope="module")
def not_clean() -> tuple[bytes, bytes]:
    html = build("gaps_and_failures_hard_strings")["report.html"]
    return html, render(html)


def test_metadata_ids_and_no_active_content(not_clean: tuple[bytes, bytes]) -> None:
    html, pdf = not_clean
    assert complete_pdf(pdf)
    r = pdfcheck.reader(pdf)
    assert bytes(r.trailer["/ID"][0].original_bytes) == hashlib.sha256(html).digest()[:16]
    info = r.metadata or {}
    assert SEALED in str(info["/CreationDate"]) and SEALED in str(info["/ModDate"])
    assert str(info["/Producer"]) == "WeasyPrint 70.0"
    keys = pdfcheck.all_keys(r)
    assert not [k for k in pdfcheck.BANNED if k in keys]
    assert "/OutputIntents" in keys  # PDF/A output intent (sRGB2014)
    xml = r.trailer["/Root"]["/Metadata"].get_object().get_data().decode()
    assert 'pdfaid:part="2"' in xml or "<pdfaid:part>2" in xml, xml[:500]
    assert b"/FlateDecode" in pdf  # compressed streams (§5.6)


def test_only_vendored_fonts_all_subset(not_clean: tuple[bytes, bytes]) -> None:
    fonts = pdfcheck.fonts(pdfcheck.reader(not_clean[1]))
    assert fonts
    vendored = ("NotoSans", "NotoSansMono", "NotoSansArabic", "NotoSansHebrew", "NotoSansDevanagari",
                "NotoSansThai", "NotoSansSC", "NotoSansKR", "NotoEmoji")  # fmt: skip
    for f in fonts:
        tag, _, name = f.lstrip("/").partition("+")
        assert re.fullmatch(r"[A-Z]{6}", tag), f  # subset on embed, never the full font
        family = name.replace("-", "").split("Regular")[0].split("Bold")[0]
        assert family in vendored, f


def test_the_banner_is_on_every_page_with_the_page_identity(not_clean: tuple[bytes, bytes]) -> None:
    pages = pdfcheck.texts(pdfcheck.reader(not_clean[1]))
    assert len(pages) >= 2
    for i, text in enumerate(pages, 1):
        assert "NOT COMPLETE" in text, i  # an excerpted page cannot look clean (§4.2)
        assert "0192f3a4-7c1e-7d2a-9b5e-3f0c2d1e4a5b" in text, i
        assert f"page {i} of {len(pages)}" in text, i


def test_the_clean_banner_on_every_page_of_a_clean_report() -> None:
    pages = pdfcheck.texts(pdfcheck.reader(render(build("clean")["report.html"])))
    assert all("Complete: every unit reconciled against the source" in p for p in pages)


def test_uncovered_characters_show_their_marker(not_clean: tuple[bytes, bytes]) -> None:
    text = "\n".join(pdfcheck.texts(pdfcheck.reader(not_clean[1])))
    assert "U+10000" in text and "U+13000" in text and "\U00010000" not in text


def _page(*cells: str) -> bytes:
    body = "".join(f"<p>{rhtml.user(c)}</p>" for c in cells)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>t</title>'
            f"<style>{rhtml.STYLE}</style></head><body>{body}</body></html>").encode()  # fmt: skip


def test_literal_marker_text_and_a_real_control_render_differently() -> None:
    """Fix 1 in the PDF: the literal text keeps its brackets and no box; the replaced character is
    a boxed marker (a filled rectangle behind it) without brackets."""
    literal, real = render(_page("x [U+202E] y")), render(_page("x ‮ y"))
    lt = "".join(pdfcheck.texts(pdfcheck.reader(literal)))
    rt = "".join(pdfcheck.texts(pdfcheck.reader(real)))
    assert "[U+202E]" in lt and "[U+202E]" not in rt and "U+202E" in rt

    def rects(pdf: bytes) -> int:
        page = pdfcheck.reader(pdf).pages[0]
        return len(re.findall(rb"\bre\b", page.get_contents().get_data()))

    assert rects(real) > rects(literal)  # the marker's background box is drawn


def test_bytes_do_not_depend_on_the_environment() -> None:
    """Separate child processes with different TZ, locale, hash seed and HOME: identical bytes."""
    html = build("archive")["report.html"]
    base = {"PATH": "/opt/edisc/venv/bin:/usr/bin:/bin", "FONTCONFIG_FILE": "/opt/edisc/fonts.conf"}
    envs = [{**base, "TZ": "UTC", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0", "HOME": "/nonexistent/h1"},
            {**base, "TZ": "Asia/Kathmandu", "LANG": "POSIX", "PYTHONHASHSEED": "12345",
             "HOME": "/nonexistent/h2"}]  # fmt: skip
    assert len({hashlib.sha256(render(html, env=e)).hexdigest() for e in envs}) == 1


def test_a_child_over_its_memory_limit_fails_the_render_cleanly() -> None:
    """§6: RLIMIT_AS on the CHILD; the render fails as a retryable error, nothing returned."""
    with pytest.raises(ReportPdfRenderError):
        render(build("at_the_cap")["report.html"], memory=300 * 2**20)


def test_html_over_the_cap_is_refused_by_the_child() -> None:
    with pytest.raises(ReportPdfRenderError, match="exited 4"):
        render(b"<p>" + b"x" * (5 << 20))
