"""An exclusive lock on the ephemeral integration-test stack (project ``edisc-test``).

Two integration runs on one machine would share one Postgres, one MinIO and one Temporal on the same
fixed ports: cross-contamination, and the likeliest cause of the undiagnosed failure in ADR 0015
§24.6 (a run overlapping other work). So every target that owns the stack takes this lock first and a
second run FAILS FAST with a clear message instead of quietly sharing.

The lock is a small JSON file (its path from ``EDISC_TEST_STACK_LOCK``, default under ``TMPDIR``): the
owning run creates it EXCLUSIVELY when it brings the stack up (``test-env-up``) and removes it when it
tears the stack down (``test-env-down``). It is a plain marker, not a process lock: nothing stays alive
for the stack's whole lifetime (the up / only / down workflow is three separate ``make`` runs), so the
lock is NOT keyed on a live pid and is never auto-stolen -- stealing is exactly how two runs would end
up sharing a stack. A lock left by a crashed run is cleared by ``make test-env-down`` (which always
releases) or by removing the file; the acquire message says so.

Each CI runner is its own VM with its own ``TMPDIR``, so this never makes two CI jobs collide -- the
integration shards (one job per runner) each own their own stack.

A second hazard is two RUNS on one up stack (``make test-integration-only`` twice): each starts by
wiping the shared ``--basetemp``, so the second deletes the first one's files mid-run (the dev IdP key
among them: found 2026-10-14 as ``InvalidSignatureError`` 401s in an unrelated test). ``run`` holds
an exclusive ``flock`` on ``<path>`` for the whole command: the kernel releases it when the process
ends, however it ends, so a dead run never blocks the next one and a live one is never shared.

Subcommands:
  acquire <path> [--label L]  create the lock; exit 3 (with who holds it) if it already exists.
  release <path>              remove the lock (idempotent).
  require <path>              exit 4 if no lock exists (no stack is up).
  run <path> [--fresh-dir D] -- CMD...
                              run CMD under an exclusive flock on <path> (exit 5 if another run holds
                              it), after recreating directory D empty; exit with CMD's status.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


def acquire(path: Path, label: str) -> int:
    payload = json.dumps({
        "pid": os.getpid(), "host": socket.gethostname(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "label": label,
    })  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            owner = json.loads(path.read_text())
            who = f"{owner['label']} (started {owner['started']} on {owner['host']})"
        except (OSError, ValueError, KeyError):
            who = "another run"
        print(
            f"the edisc-test stack is already in use by {who}.\n"
            f"  a second integration run cannot share it (one Postgres/MinIO/Temporal on fixed "
            f"ports).\n"
            f"  wait for that run to finish; if it crashed, `make test-env-down` clears the stack and "
            f"this lock (or remove {path}).",
            file=sys.stderr,
        )
        return 3
    with os.fdopen(fd, "w") as fh:
        fh.write(payload)
    return 0


def release(path: Path) -> int:
    path.unlink(missing_ok=True)
    return 0


def require(path: Path) -> int:
    if path.exists():
        return 0
    print(
        f"no edisc-test stack is up (lock {path} is missing): run `make test-env-up` first",
        file=sys.stderr,
    )
    return 4


def run(path: Path, command: list[str], fresh_dir: Path | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                "another integration run is using the edisc-test stack right now: wait for it to"
                " finish (two runs would wipe each other's --basetemp)",
                file=sys.stderr,
            )
            return 5
        if fresh_dir is not None:
            shutil.rmtree(fresh_dir, ignore_errors=True)
            fresh_dir.mkdir(parents=True)
        # the Makefile's own command line (no outside input); the lock is held until it ends
        return subprocess.run(command, check=False).returncode  # noqa: S603


def main() -> int:
    argv = sys.argv[1:]
    command: list[str] = []
    if "--" in argv:
        command, argv = argv[argv.index("--") + 1 :], argv[: argv.index("--")]
    ap = argparse.ArgumentParser(prog="stack_lock")
    ap.add_argument("action", choices=["acquire", "release", "require", "run"])
    ap.add_argument("path", type=Path)
    ap.add_argument("--label", default="integration run")
    ap.add_argument("--fresh-dir", type=Path)
    args = ap.parse_args(argv)
    if args.action == "run":
        if not command:
            ap.error("run needs -- CMD...")
        return run(args.path, command, args.fresh_dir)
    if args.action == "acquire":
        return acquire(args.path, args.label)
    if args.action == "release":
        return release(args.path)
    return require(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
