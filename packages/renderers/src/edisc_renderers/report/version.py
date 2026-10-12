"""Versions of the collection report (ADR 0018 §3, §5.7)."""

REPORT_FORMAT = "edisc-collection-report/1"

REPORT_RENDERER_VERSION = "1.5.0"
"""Byte-identical `report.json`, JSONL files and `report.html` are promised for the same inputs AND
this version. Any change to their bytes needs a bump; the report goldens
(`tests/golden/report/<version>_unicode-<unicode>/`) are keyed by it.

1.0.0: model, JSONL, HTML (M16 steps 1-2). 1.1.0: code-point markers are elements, HTML-invalid
code points are replaced, the per-conversation list and `conversations.jsonl` above the cap. 1.2.0:
characters no vendored font draws are replaced (glyph coverage, §5.5), `dcterms` dates and the page
identity element for the PDF (M16 step 3). 1.3.0: lone surrogates written as replacement text plus
their exact code units and counted (`lone_surrogates`, §23); the glyph set in the identity (§24).
1.4.0: unit rows state the unit's kind, conversation, scopes, access-lost reason and gap reasons
from the chain (UNKNOWN for jobs recorded before §28), units unsettled at the end come from the chain
(`units_unsettled`), file order in code points whatever the database's locale; gap reasons and the
access-lost reason in the HTML exception table (§28). 1.5.0: for a job read from a Slack export, the
export's declared plan, detected tier, blind spots, tier warnings and validation findings from the
chain (`job_started.export`, UNKNOWN where an older job did not record them) and an Export section
in the HTML (§32)."""
