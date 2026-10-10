"""The parent side of the report PDF child (`edisc_worker.report_pdf.render_pdf`, ADR 0018 §6,
amendment 7), with FAKE children (`python -c ...`): the real child needs the report image
(tests/unit/report_image). Nothing is returned unless the child exited 0 with a complete PDF; every
other outcome raises the retryable `ReportPdfRenderError`, and a child that overruns, overflows or
whose attempt is cancelled is killed, never left running."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from edisc_worker.report_pdf import (
    EXIT_MEMORY,
    ReportPdfRenderError,
    child_command,
    child_environment,
    complete_pdf,
    render_pdf,
)

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def fake(body: str) -> list[str]:
    """A child that runs ``body`` (stdin holds the HTML)."""
    return [sys.executable, "-c", "import os, sys, time\n" + textwrap.dedent(body)]


async def run(cmd: list[str], *, limit_s: float = 20, max_bytes: int = 1 << 20,
              concurrency: int = 1) -> bytes:  # fmt: skip
    return await render_pdf(b"<html>", command=cmd, timeout_seconds=limit_s,
                            max_pdf_bytes=max_bytes, concurrency=concurrency, env=dict(os.environ))  # fmt: skip


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def gone(pid: int) -> bool:
    for _ in range(100):
        if not alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_a_complete_pdf_from_a_child_that_exits_0() -> None:
    out = await run(fake(f"sys.stdin.buffer.read(); sys.stdout.buffer.write({PDF!r})"))
    assert out == PDF


@pytest.mark.parametrize(
    ("body", "words"),
    [
        (f"sys.stdout.buffer.write({PDF[:20]!r})", "truncated"),  # killed mid-write, exit 0 anyway
        ("pass", "empty or truncated"),
        ("sys.stderr.write('boom'); sys.exit(1)", "exited 1: boom"),
        (f"sys.exit({EXIT_MEMORY})", "out of memory"),
        ("import signal; os.kill(os.getpid(), signal.SIGKILL)", "killed by SIGKILL"),
        (f"sys.stdout.buffer.write({PDF!r}); sys.exit(2)", "exited 2"),  # a PDF, but not exit 0
    ],
)
async def test_every_failure_is_a_retryable_pdf_error(body: str, words: str) -> None:
    with pytest.raises(ReportPdfRenderError, match=words):
        await run(fake(body))


async def test_an_overrunning_child_is_killed(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    t0 = time.monotonic()
    with pytest.raises(ReportPdfRenderError, match="overran"):
        await run(fake(f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"),
                  limit_s=1.5)  # fmt: skip
    assert time.monotonic() - t0 < 10
    assert await gone(int(pid_file.read_text()))


async def test_a_cancelled_attempt_kills_its_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(
        run(fake(f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"))
    )
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await gone(int(pid_file.read_text()))


async def test_output_beyond_the_bound_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    body = (f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "while True: sys.stdout.buffer.write(b'x' * 65536)")  # fmt: skip
    with pytest.raises(ReportPdfRenderError, match="more than"):
        await run(fake(body), max_bytes=1 << 20)
    assert await gone(int(pid_file.read_text()))


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_pdf_children_run_one_at_a_time_per_process(tmp_path: Path, concurrency: int) -> None:
    """`EDISC_REPORT_PDF_CONCURRENCY` (1): a second PDF waits for the first child to exit."""
    log = tmp_path / "log"
    body = (f"sys.stdin.buffer.read(); f = open({str(log)!r}, 'a'); f.write(f's {{time.monotonic()}}\\n');"
            f" f.flush(); time.sleep(0.6); f.write(f'e {{time.monotonic()}}\\n'); f.close();"
            f" sys.stdout.buffer.write({PDF!r})")  # fmt: skip
    await asyncio.gather(*(run(fake(body), concurrency=concurrency) for _ in range(2)))
    events = sorted(
        (float(t), kind) for kind, t in (line.split() for line in log.read_text().splitlines())
    )
    kinds = [k for _, k in events]
    if concurrency == 1:
        assert kinds == ["s", "e", "s", "e"], events  # never two children at once
    else:
        assert kinds == ["s", "s", "e", "e"], events  # the control: the check can fail


@pytest.mark.skipif(
    sys.platform != "linux", reason="RLIMIT_AS is enforced only on Linux (CI runs it)"
)
async def test_the_childs_memory_limit_is_its_own() -> None:
    """The limit applies to the CHILD: an allocation above it fails there (exit 3), the parent is
    untouched."""
    body = f"""
        from edisc_worker.report_pdf import _limit_memory
        _limit_memory(256 << 20)
        try:
            blob = bytearray(1 << 30)
        except MemoryError:
            sys.exit({EXIT_MEMORY})
        sys.stdout.buffer.write({PDF!r})
    """
    with pytest.raises(ReportPdfRenderError, match="out of memory"):
        await run(fake(body))
    assert len(bytearray(1 << 28)) == 1 << 28  # the parent still allocates freely


def test_the_child_gets_no_credentials_and_a_fixed_locale() -> None:
    env = child_environment({
        "PATH": "/usr/bin", "FONTCONFIG_FILE": "/opt/edisc/fonts.conf", "EDISC_ENV": "test",
        "EDISC_DATABASE_URL": "postgresql://u:p@h/db", "EDISC_S3_SECRET_KEY": "s3cret",
        "AWS_SECRET_ACCESS_KEY": "x", "TEMPORAL_ADDRESS": "t:7233", "TZ": "Asia/Kolkata",
        "LANG": "fr_FR.UTF-8",
    })  # fmt: skip
    assert set(env) == {"PATH", "FONTCONFIG_FILE", "EDISC_ENV", "PYTHONDONTWRITEBYTECODE",
                        "PYTHONHASHSEED", "LC_ALL", "TZ", "HOME"}  # fmt: skip
    assert env["TZ"] == "UTC" and env["LC_ALL"] == "C.UTF-8"


def test_the_child_command_and_completeness() -> None:
    cmd = child_command("/opt/edisc/venv/bin/python", "a4", 2 << 30, 4 << 20)
    assert cmd[:3] == ["/opt/edisc/venv/bin/python", "-m", "edisc_worker.report_pdf"]
    assert "--paper" in cmd and "a4" in cmd and str(2 << 30) in cmd
    assert complete_pdf(PDF) and complete_pdf(PDF + b"\n") and not complete_pdf(PDF[:-7])
    assert not complete_pdf(b"") and not complete_pdf(b"%%EOF")
