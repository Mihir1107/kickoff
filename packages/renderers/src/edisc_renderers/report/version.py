"""Versions of the collection report (ADR 0018 §3, §5.7)."""

REPORT_FORMAT = "edisc-collection-report/1"

REPORT_RENDERER_VERSION = "1.0.0"
"""Byte-identical `report.json` and JSONL files are promised for the same inputs AND this version.
Any change to their bytes needs a bump; the report goldens are keyed by it."""
