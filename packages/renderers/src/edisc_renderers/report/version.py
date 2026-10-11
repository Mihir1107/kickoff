"""Versions of the collection report (ADR 0018 §3, §5.7)."""

REPORT_FORMAT = "edisc-collection-report/1"

REPORT_RENDERER_VERSION = "1.3.0"
"""Byte-identical `report.json`, JSONL files and `report.html` are promised for the same inputs AND
this version. Any change to their bytes needs a bump; the report goldens
(`tests/golden/report/<version>_unicode-<unicode>/`) are keyed by it.

1.0.0: model, JSONL, HTML (M16 steps 1-2). 1.1.0: code-point markers are elements, HTML-invalid
code points are replaced, the per-conversation list and `conversations.jsonl` above the cap. 1.2.0:
characters no vendored font draws are replaced (glyph coverage, §5.5), `dcterms` dates and the page
identity element for the PDF (M16 step 3). 1.3.0: lone surrogates written as replacement text plus
their exact code units and counted (`lone_surrogates`, §23); the glyph set in the identity (§24)."""
