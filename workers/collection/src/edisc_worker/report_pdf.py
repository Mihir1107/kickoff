"""The report PDF, rendered in a CHILD PROCESS with its own memory limit (ADR 0018 §6, amendment 7).

A thread can be neither cancelled nor memory-limited: a timed-out attempt's render would keep running
beside its retry, and an out-of-memory render would take the whole worker (and every activity on it)
down. So the activity spawns one child per PDF:

    python -m edisc_worker.report_pdf --paper letter --memory-bytes N   < report.html  > report.pdf

The child (`main`) applies `RLIMIT_AS` = N and `oom_score_adj` = 1000 BEFORE importing WeasyPrint (if
the container's cgroup limit is reached anyway, the kernel kills the child, not the worker), checks
that WeasyPrint's bundled ICC profile is the vendored one, reads the STORED HTML on stdin and writes
the PDF on stdout, nothing else: it gets no DB, S3 or Temporal credentials (a minimal environment)
and writes no file. PDF/A-2u, compressed streams (§5.6), `/ID` first half = the first 16 bytes of
SHA-256(HTML), dates from the HTML's `dcterms` metadata (the job's `sealed_at`), every remote fetch
refused, the constant print stylesheet of the report's paper.

The parent (`render_pdf`) awaits the child asynchronously (the loop keeps heartbeating), holds a
process-wide slot (`EDISC_REPORT_PDF_CONCURRENCY`, 1), enforces a wall-clock limit, bounds the output,
and kills the child's process group when the attempt is cancelled or times out. Any death (signal,
`MemoryError`, non-zero exit), overrun, or empty / truncated / oversized output raises
`ReportPdfRenderError` (retryable); nothing is returned unless the child exited 0 with a complete PDF.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import signal
import sys
import weakref
from collections.abc import Mapping, Sequence
from typing import Any

PDF_MODULE = "edisc_worker.report_pdf"
EXIT_MEMORY = 3
EXIT_REFUSED = 4
_CHUNK = 1 << 16
# what the child may see of the parent's environment: no credentials, nothing that could change bytes
_CHILD_ENV = ("PATH", "FONTCONFIG_FILE", "EDISC_REPORT_FONT_DIR", "EDISC_ENV",
              "EDISC_TEST_REPORT_PDF_HOLD_SECONDS")  # fmt: skip


class ReportPdfRenderError(RuntimeError):
    """The PDF child did not produce a complete PDF (killed, out of memory, failed, overran, or its
    output is empty, truncated or too large). Retryable: the next attempt renders again from the
    stored HTML; nothing was written."""


# ------------------------------------------------------------------ the child
def render_pdf_bytes(html: bytes, paper: str) -> bytes:
    """The rendering itself (child process only: imports WeasyPrint)."""
    from weasyprint import (  # type: ignore[import-untyped]
        CSS,
        HTML,
    )

    from edisc_renderers.report.print_css import print_stylesheet

    def refuse(url: str, *args: Any, **kwargs: Any) -> Any:
        raise ValueError(f"the report PDF fetches nothing (refused {url[:80]!r})")

    document = HTML(string=html.decode("utf-8"), url_fetcher=refuse)
    out: bytes = document.write_pdf(
        stylesheets=[CSS(string=print_stylesheet(paper), url_fetcher=refuse)],
        pdf_identifier=hashlib.sha256(html).digest()[:16],
        pdf_variant="pdf/a-2u",
        uncompressed_pdf=False,
    )
    return out


def _limit_memory(memory_bytes: int) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    # Linux only; if the container's cgroup limit is reached, the OOM killer then picks the child
    with contextlib.suppress(OSError), open("/proc/self/oom_score_adj", "w") as f:
        f.write("1000")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=PDF_MODULE)
    ap.add_argument("--paper", required=True)
    ap.add_argument("--memory-bytes", type=int, required=True)
    ap.add_argument("--max-html-bytes", type=int, default=4 << 20)
    args = ap.parse_args(argv)
    try:
        _limit_memory(args.memory_bytes)  # before anything large is imported or allocated
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"cannot apply the memory limit: {exc}" + "\n")
        return EXIT_REFUSED
    hold = os.environ.get("EDISC_TEST_REPORT_PDF_HOLD_SECONDS")
    if hold and os.environ.get("EDISC_ENV") in ("local", "test", "ci"):
        import time

        time.sleep(float(hold))  # TEST ONLY: hold the child (heartbeats must keep flowing)
    html = sys.stdin.buffer.read(args.max_html_bytes + 1)
    if len(html) > args.max_html_bytes:
        sys.stderr.write(f"report.html exceeds {args.max_html_bytes} bytes" + "\n")
        return EXIT_REFUSED
    try:
        from edisc_worker.versions import check_icc

        check_icc()
        pdf = render_pdf_bytes(html, args.paper)
    except MemoryError:
        sys.stderr.write("out of memory (RLIMIT_AS) while rendering" + "\n")
        return EXIT_MEMORY
    sys.stdout.buffer.write(pdf)
    sys.stdout.buffer.flush()
    return 0


# ------------------------------------------------------------------ the parent
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)


def _slot(concurrency: int) -> asyncio.Semaphore:
    """The process-wide PDF slots: one semaphore per event loop (tests run many loops) and size
    (production has one size, `EDISC_REPORT_PDF_CONCURRENCY`)."""
    per_loop = _slots.setdefault(asyncio.get_running_loop(), {})
    sem = per_loop.get(concurrency)
    if sem is None:
        sem = per_loop[concurrency] = asyncio.Semaphore(concurrency)
    return sem


def child_command(python: str, paper: str, memory_bytes: int, max_html_bytes: int) -> list[str]:
    return [python, "-m", PDF_MODULE, "--paper", paper, "--memory-bytes", str(memory_bytes),
            "--max-html-bytes", str(max_html_bytes)]  # fmt: skip


def child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    env = {k: environ[k] for k in _CHILD_ENV if k in environ}
    env |= {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "LC_ALL": "C.UTF-8",
            "TZ": "UTC", "HOME": "/nonexistent"}  # fmt: skip
    return env


def complete_pdf(data: bytes) -> bool:
    """A PDF that was written to the end: header, and `%%EOF` as its last token."""
    return data.startswith(b"%PDF-") and data.rstrip(b"\r\n\t ").endswith(b"%%EOF")


async def render_pdf(
    html: bytes, *, command: Sequence[str], timeout_seconds: float, max_pdf_bytes: int,
    concurrency: int = 1, env: Mapping[str, str] | None = None,
) -> bytes:  # fmt: skip
    """Run ``command`` (normally `child_command`) with ``html`` on stdin; the PDF from its stdout."""
    async with _slot(concurrency):
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
            env=child_environment(os.environ) if env is None else dict(env),
        )  # fmt: skip
        try:
            out, err = await asyncio.wait_for(_exchange(proc, html, max_pdf_bytes), timeout_seconds)
        except TimeoutError as exc:
            await _kill(proc)
            raise ReportPdfRenderError(
                f"the PDF child overran {timeout_seconds:g} s and was killed"
            ) from exc
        except BaseException:  # cancelled (attempt timed out or worker stopping), oversize, ...
            await _kill(proc)
            raise
    code = proc.returncode
    tail = err[-2000:].decode("utf-8", "replace").strip()
    if code is not None and code < 0:
        raise ReportPdfRenderError(
            f"the PDF child was killed by {signal.Signals(-code).name}: {tail}"
        )
    if code == EXIT_MEMORY:
        raise ReportPdfRenderError(f"the PDF child ran out of memory: {tail}")
    if code != 0:
        raise ReportPdfRenderError(f"the PDF child exited {code}: {tail}")
    if not complete_pdf(out):
        raise ReportPdfRenderError(
            f"the PDF child's output is empty or truncated ({len(out)} bytes)"
        )
    return out


async def _exchange(
    proc: asyncio.subprocess.Process, html: bytes, max_pdf_bytes: int
) -> tuple[bytes, bytes]:
    async def feed() -> None:
        stdin = proc.stdin
        assert stdin is not None  # noqa: S101
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            for i in range(0, len(html), _CHUNK):
                stdin.write(html[i : i + _CHUNK])
                await stdin.drain()
            stdin.close()

    async def read_out() -> bytes:
        stdout = proc.stdout
        assert stdout is not None  # noqa: S101
        chunks: list[bytes] = []
        size = 0
        while chunk := await stdout.read(_CHUNK):
            size += len(chunk)
            if size > max_pdf_bytes:
                raise ReportPdfRenderError(f"the PDF child wrote more than {max_pdf_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    async def read_err() -> bytes:
        stderr = proc.stderr
        assert stderr is not None  # noqa: S101
        return (await stderr.read(1 << 20))[-8192:]

    try:
        async with asyncio.TaskGroup() as tg:  # one failing part cancels the others
            tg.create_task(feed())
            out_task, err_task = tg.create_task(read_out()), tg.create_task(read_err())
    except* ReportPdfRenderError as group:
        raise group.exceptions[0] from None
    await proc.wait()
    return out_task.result(), err_task.result()


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's process group and reap it, even while being cancelled."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    async def reap() -> None:
        # asyncio's wait() also waits for the pipes to close: drain them, nobody else reads now
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                while await stream.read(_CHUNK):
                    pass
        await proc.wait()

    with contextlib.suppress(Exception):
        await asyncio.shield(reap())


if __name__ == "__main__":
    raise SystemExit(main())
