"""Tests that need the pinned linux/amd64 report image (ADR 0018 §5.8, §6): the real PDF child,
the toolchain id, the PDF goldens (the image's admission gate), font isolation. Anywhere else they
are SKIPPED WITH A VISIBLE REASON, never silently passed. Run them with `make test-report-pdf`
(builds the image, runs them inside it, then veraPDF); CI runs them natively on amd64."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from edisc_worker.report_pdf import child_command, child_environment, render_pdf

CHILD_PYTHON = "/opt/edisc/venv/bin/python"  # the worker's interpreter: never the test venv
FONT_DIR = Path("/opt/edisc/fonts")
IN_IMAGE = Path(CHILD_PYTHON).exists() and os.environ.get("EDISC_REPORT_FONT_DIR") == str(FONT_DIR)
REASON = "PDF goldens need the pinned linux/amd64 report image: run make test-report-pdf"
GOLDEN = Path(__file__).resolve().parents[2] / "golden"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if IN_IMAGE:
        return
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.skip(reason=REASON))


def render(html: bytes, paper: str = "letter", *, memory: int = 5 * 2**29,
           env: Mapping[str, str] | None = None) -> bytes:  # fmt: skip
    """The production child, as the worker runs it."""
    return asyncio.run(
        render_pdf(
            html, command=child_command(CHILD_PYTHON, paper, memory, 4 << 20),
            timeout_seconds=900, max_pdf_bytes=64 << 20,
            env=child_environment(os.environ) if env is None else env,
        )
    )  # fmt: skip


@cache
def toolchain() -> tuple[str, dict[str, Any]]:
    from edisc_worker.versions import measure

    return measure(CHILD_PYTHON)
