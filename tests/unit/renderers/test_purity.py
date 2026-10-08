"""The renderer is pure (ADR 0015 §1): no database, object storage, workflow or clock code."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import edisc_renderers.rsmf as rsmf

FORBIDDEN_MODULES = (
    "sqlalchemy", "asyncpg", "psycopg", "boto3", "botocore", "aiobotocore", "aioboto3",
    "temporalio", "redis", "httpx", "edisc_db", "edisc_evidence", "edisc_worker", "edisc_api",
)  # fmt: skip
FORBIDDEN_CALLS = re.compile(
    r"\b(utc_now|datetime\.now|date\.today|time\.time|time\.monotonic|uuid[14]|random|secrets|os\.environ)\b"
)


def test_importing_the_renderer_loads_no_db_or_cloud_code() -> None:
    code = (
        "import sys, edisc_renderers.rsmf; "
        f"bad = [m for m in sys.modules if m.split('.')[0] in {FORBIDDEN_MODULES!r}]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_clock_randomness_or_environment_in_the_source() -> None:
    root = pathlib.Path(rsmf.__file__).parent
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for path in sorted(root.glob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if FORBIDDEN_CALLS.search(line.split("#")[0])
    ]
    assert offenders == []


def test_the_report_model_and_html_are_pure_too() -> None:
    """`edisc_renderers.report` (model and the HTML builder) is imported by the offline verifier
    (ADR 0018 §14) and must stay DB/S3/clock-free (§3.2)."""
    import edisc_renderers.report.model as report_model

    code = (
        "import sys, edisc_renderers.report.model, edisc_renderers.report.html; "
        f"bad = [m for m in sys.modules if m.split('.')[0] in {FORBIDDEN_MODULES!r}]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    root = pathlib.Path(report_model.__file__).parent
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for path in sorted(root.glob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if FORBIDDEN_CALLS.search(line.split("#")[0])
    ]
    assert offenders == []
