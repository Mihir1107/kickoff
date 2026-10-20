"""Every local test run leaves its full output on disk, like CI's artifacts (CLAUDE.md "Commands").

A failure seen once and lost with the terminal scrollback cannot be diagnosed (ADR 0015 §24.6 and
§24.21 were both "output lost"). So every ``make`` test target runs its command through this:

    uv run python scripts/test_log.py --label <name> [--on-failure "<shell command>"] -- CMD...

- ``test-runs/<UTC time>-<label>/output.log``: everything CMD printed (stdout and stderr, in the
  order written, ANSI colour codes removed), with the command, the start and end times and the exit
  status. The terminal still shows the live output (in colour when it is a terminal).
- ``EDISC_TEST_ARTIFACTS_DIR`` and ``EDISC_TEST_PHASE_TIMINGS_DIR`` default to ``artifacts/`` and
  ``phase-timings/`` in the same directory, so a failing integration test leaves its worker logs,
  Temporal histories and custody streams (``tests/integration/artifacts.py``) exactly as on CI. An
  explicit value (CI sets both) wins.
- ``--on-failure``: run when CMD fails (the test stack's logs), its output saved as ``on-failure.log``.
- Pruned after every run, newest first: at most ``TEST_RUNS_KEEP`` (default 20) runs and
  ``TEST_RUNS_MAX_MB`` (default 1024) MiB in total. 20 runs cover a working session's history
  (diagnosing an intermittent failure means comparing it with the passing runs around it); the cap
  keeps a few failed full suites (their artifacts: worker logs, Temporal histories, custody streams,
  tens to hundreds of MiB each) within a small share of the margin above the disk guard
  (``MIN_FREE_GB``, 15). The newest run is always kept, however large (it is the one to read), and
  a run still being written (no ``# finished`` line, younger than two days) is never removed: a
  concurrent run's directory must not vanish under it.
- ``test-runs/latest`` points at the newest one.

The exit status is CMD's.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(os.environ.get("TEST_RUNS_DIR", Path(__file__).resolve().parent.parent / "test-runs"))
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


IN_PROGRESS_MAX_AGE_S = 2 * 86400  # older without a "# finished" line: killed, prunable


def _size(run: Path) -> int:
    return sum(f.stat().st_size for f in run.rglob("*") if f.is_file() and not f.is_symlink())


def _in_progress(run: Path) -> bool:
    log = run / "output.log"
    try:
        if time.time() - log.stat().st_mtime > IN_PROGRESS_MAX_AGE_S:
            return False
        with log.open("rb") as fh:
            fh.seek(max(0, log.stat().st_size - 512))
            return b"# finished:" not in fh.read()
    except FileNotFoundError:
        return time.time() - run.stat().st_mtime <= IN_PROGRESS_MAX_AGE_S


def _prune(keep: int, max_bytes: int, current: Path) -> None:
    runs = sorted((p for p in ROOT.iterdir() if p.is_dir() and not p.is_symlink()), reverse=True)
    kept, total = 0, 0
    for run in runs:  # newest first
        size = _size(run)
        if run == current or (kept < keep and total + size <= max_bytes) or kept == 0:
            kept, total = kept + 1, total + size
        elif not _in_progress(run):
            shutil.rmtree(run, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--on-failure", default=None)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("no command")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.label)
    run_dir = ROOT / f"{stamp}-{label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    latest = ROOT / "latest"
    with contextlib.suppress(OSError):
        latest.unlink(missing_ok=True)
        latest.symlink_to(run_dir.name)

    env = dict(os.environ)
    env.setdefault("EDISC_TEST_ARTIFACTS_DIR", str(run_dir / "artifacts"))
    env.setdefault("EDISC_TEST_PHASE_TIMINGS_DIR", str(run_dir / "phase-timings"))
    if sys.stdout.isatty():
        env.setdefault("PY_COLORS", "1")  # pytest writes colour to a pipe only when asked

    log_path = run_dir / "output.log"
    started = time.monotonic()
    with log_path.open("wb") as log:
        log.write(f"# command: {' '.join(cmd)}\n# started: {_now()}\n".encode())
        log.flush()
        # the developer's own make target command line
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # noqa: S603
        if proc.stdout is None:
            raise RuntimeError("no pipe from the command")
        out = sys.stdout.buffer
        pending = b""  # an escape sequence may be split across reads
        try:
            while chunk := os.read(proc.stdout.fileno(), 65536):
                out.write(chunk)
                out.flush()
                data = pending + chunk
                cut = data.rfind(b"\x1b")
                if cut != -1 and not ANSI.search(data, cut):
                    data, pending = data[:cut], data[cut:]
                else:
                    pending = b""
                log.write(ANSI.sub(b"", data))
                log.flush()
        except KeyboardInterrupt:
            # the child got the same SIGINT (one process group): let it print its summary
            proc.send_signal(signal.SIGINT)
            rest = proc.stdout.read()
            out.write(rest)
            log.write(ANSI.sub(b"", pending + rest))
            pending = b""
        status = proc.wait()
        log.write(ANSI.sub(b"", pending))
        log.write(
            f"\n# finished: {_now()} after {time.monotonic() - started:.1f} s,"
            f" exit status {status}\n".encode()
        )

    if status != 0 and args.on_failure:
        with (run_dir / "on-failure.log").open("wb") as fh:
            # a shell command line from the Makefile (the stack's logs)
            subprocess.run(  # noqa: S602
                args.on_failure, shell=True, stdout=fh, stderr=subprocess.STDOUT, check=False
            )

    keep = int(os.environ.get("TEST_RUNS_KEEP", "20"))
    max_mb = int(os.environ.get("TEST_RUNS_MAX_MB", "1024"))
    _prune(keep, max_mb * 1024 * 1024, run_dir)
    print(f"test output saved to {log_path}", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
