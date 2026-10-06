"""The slice rendering that runs in a worker thread (`render_store._render_slice`) is pure compute.

A thread cannot be cancelled: when Temporal times out or cancels an attempt, the thread of that
attempt keeps running until the slice is done, next to the retry. So it must not be able to write
anything. Checked with an audit hook (in a subprocess, since audit hooks cannot be removed): while
`_render_slice` runs, no thread other than the loop's opens a file for writing, touches the
filesystem, opens a socket or starts a process. Every write (natives, productions, custody) happens
on the loop, after the thread has returned.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import asyncio, os, sys, threading
    from datetime import date
    from edisc_renderers.rsmf import RenderOptions, render_slice
    from edisc_worker import render_store
    from edisc_worker.pipeline import CrashHooks
    from tests.unit.renderers.builders import attachment, msg, slice_input, unavailable

    data = b"\\x00\\x01binary" * 100
    inp = slice_input(
        date(2026, 1, 5),
        [msg("2026-01-05T09:00:00Z", files=("F1", "F2")), msg("2026-01-05T10:00:00Z", text="b")],
        files={"F1": attachment("F1", "a.pdf", data), "F2": unavailable("F2", "b.png", "expired_url")},
    )
    options = RenderOptions()
    render_slice(inp, options)  # warm every lazy import and cache on the main thread first

    WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    MUTATING = ("os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.truncate", "os.chmod",
                "os.symlink", "os.link", "shutil.", "subprocess.", "os.system", "os.exec",
                "os.posix_spawn", "os.fork", "socket.")
    main = threading.get_ident()
    seen, ran = [], []

    def hook(event, args):
        if threading.get_ident() == main:
            return
        if event == "edisc.render_thread":
            ran.append(1)
        elif event == "open":
            path, mode, flags = args
            if (mode and any(c in mode for c in "wax+")) or (flags or 0) & WRITE:
                seen.append(f"open {path} {mode} {flags}")
        elif event.startswith(MUTATING):
            seen.append(f"{event} {args!r}")

    sys.addaudithook(hook)

    class Probe(CrashHooks):
        def block(self, point):
            sys.audit("edisc.render_thread")

    files = asyncio.run(render_store._render_slice(inp, options, Probe()))
    assert ran == [1], "the rendering did not run in a worker thread"
    assert files, "nothing rendered"
    print("\\n".join(seen))
    sys.exit(1 if seen else 0)
    """
)


def test_the_render_thread_writes_nothing() -> None:
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
