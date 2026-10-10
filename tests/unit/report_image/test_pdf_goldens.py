"""PDF goldens: the ADMISSION GATE of a report image (ADR 0018 §5.8, §6 amendment 4).

`tests/golden/report-pdf/<report key>_toolchain-<id12>/index.json` holds, per case of the report
goldens (`tests/golden/report/<report key>/<case>/report.html`, pinned first) and per paper (letter,
a4), the SHA-256 and size of the PDF the image renders. An image is admitted to serve a report
identity only if every PDF matches BYTE FOR BYTE (plus veraPDF in CI): no tolerance, and no
re-recording to make an image pass. An image whose toolchain id has no recorded generation is NOT
admitted (this test fails; it never passes silently).

Record a generation, by hand, inside the image (never in CI, never over an existing one):

    make test-report-pdf EDISC_RECORD_REPORT_PDF=1

`EDISC_REPORT_PDF_OUT=<dir>` also writes every rendered PDF there (veraPDF, CI artifacts).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.unit.renderers.test_report_golden import CASES, build, key

from .conftest import GOLDEN, render, toolchain

PAPERS = ("letter", "a4")


def generation() -> Path:
    return GOLDEN / "report-pdf" / f"{key()}_toolchain-{toolchain()[0][:12]}"


def _recording() -> bool:
    if os.environ.get("EDISC_RECORD_REPORT_PDF") != "1":
        return False
    if os.environ.get("CI"):
        pytest.fail("PDF goldens are never recorded in CI: record by hand in the image and commit")
    return True


@pytest.fixture(scope="module")
def rendered() -> dict[str, dict[str, dict[str, object]]]:
    out_dir = os.environ.get("EDISC_REPORT_PDF_OUT")
    index: dict[str, dict[str, dict[str, object]]] = {}
    for case in sorted(CASES):
        html = build(case)["report.html"]
        recorded_html = GOLDEN / "report" / key() / case / "report.html"
        assert recorded_html.read_bytes() == html, f"{case}: report.html golden first (§5.8)"
        index[case] = {}
        for paper in PAPERS:
            pdf = render(html, paper)
            index[case][paper] = {"sha256": hashlib.sha256(pdf).hexdigest(), "size": len(pdf)}
            if out_dir:
                Path(out_dir, f"{case}-{paper}.pdf").write_bytes(pdf)
    return index


def test_pdf_goldens_match_byte_for_byte(rendered: dict[str, dict[str, dict[str, object]]]) -> None:
    gen = generation()
    index_file = gen / "index.json"
    doc = {"report_key": key(), "toolchain_id": toolchain()[0], "pdfs": rendered}
    if not index_file.exists():
        if not _recording():
            pytest.fail(
                f"NOT ADMITTED: no PDF goldens for toolchain {toolchain()[0]} ({gen.name}); record"
                " them by hand inside the image (EDISC_RECORD_REPORT_PDF=1) only for a deliberate"
                " new image or report renderer"
            )
        gen.mkdir(parents=True)
        index_file.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        (gen / "toolchain.json").write_text(
            json.dumps(toolchain()[1], indent=1, sort_keys=True) + "\n"
        )
        return
    _recording()  # refuses in CI; an existing generation is only compared, never rewritten
    assert json.loads(index_file.read_text()) == doc, "PDF bytes differ: the image is NOT admitted"
