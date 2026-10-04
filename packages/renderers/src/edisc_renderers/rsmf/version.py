"""Versions written into every file (ADR 0015 §5, §6)."""

RSMF_VERSION = "2.0.0"

RENDERER_VERSION = "1.1.0"
"""Byte-identical output is promised for the same inputs AND this version. Any change to the output
bytes needs a bump; the golden tests (`tests/golden/rsmf/<version>/`) are keyed by it."""
