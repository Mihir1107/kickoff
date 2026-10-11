"""Versions written into every file (ADR 0015 §5, §6)."""

RSMF_VERSION = "2.0.0"

RENDERER_VERSION = "1.3.2"
"""Byte-identical output is promised for the same inputs AND this version. Any change to the output
bytes needs a bump; the golden tests (`tests/golden/rsmf/<version>/`) are keyed by it.
1.3.2: lone surrogates (ADR 0018 §23) written as U+FFFD with their exact code units in
`edisc.lone_surrogates` (before: the render failed); other output unchanged."""
