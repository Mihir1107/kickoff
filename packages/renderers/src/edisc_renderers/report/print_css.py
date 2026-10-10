"""The constant print stylesheet the PDF child applies to the STORED `report.html` (ADR 0018 §5.1, §4.2).
Pure. Its SHA-256 (every paper) is part of the PDF toolchain id (§5.3), and it is renderer code: a
change is a report renderer bump AND a new toolchain id.

- `@page` size from the paper of the report's identity (§5.7: `letter`, `a4`).
- The banner is a RUNNING element: the same words appear in the header of EVERY page (§4.2: an
  excerpted page cannot look clean). The footer carries the page identity element (job id and
  snapshot digest) and "page X of Y".
- Only vendored font families (§5.4); code-point markers in the monospace font.
"""

from __future__ import annotations

import hashlib

PAPERS = {"letter": "Letter", "a4": "A4"}
FAMILIES = (
    '"Noto Sans", "Noto Sans Arabic", "Noto Sans Hebrew", "Noto Sans Devanagari", "Noto Sans Thai",'
    ' "Noto Sans SC", "Noto Sans KR", "Noto Emoji"'
)


def print_stylesheet(paper: str) -> str:
    if paper not in PAPERS:
        raise ValueError(f"unknown paper {paper!r}")
    return (
        f"@page{{size:{PAPERS[paper]};margin:30mm 12mm 16mm 12mm;"
        "@top-center{content:element(banner);width:100%;vertical-align:bottom}"
        "@bottom-left{content:element(pageid);vertical-align:top}"
        '@bottom-right{content:"page " counter(page) " of " counter(pages);'
        'font:7pt "Noto Sans";vertical-align:top}}'
        f"html{{font-family:{FAMILIES} !important;font-size:7.5pt !important;color:#000 !important}}"
        "body{max-width:none !important;padding:0 !important;margin:0 !important}"
        ".banner{position:running(banner);font-size:8pt !important;margin:0 !important}"
        '.pageid{position:running(pageid);font:7pt "Noto Sans Mono" !important;margin:0 !important}'
        '.cp{font-family:"Noto Sans Mono" !important}'
        "h1{font-size:12pt !important}h2{font-size:9.5pt !important}"
        "table{page-break-inside:auto}tr{page-break-inside:avoid}"
    )


def print_stylesheets_sha256() -> str:
    """One digest over every paper's stylesheet, for the toolchain id."""
    h = hashlib.sha256()
    for paper in sorted(PAPERS):
        h.update(paper.encode() + b"\0" + print_stylesheet(paper).encode("utf-8") + b"\0")
    return h.hexdigest()
