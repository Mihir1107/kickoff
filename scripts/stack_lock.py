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

Subcommands:
  acquire <path> [--label L]  create the lock; exit 3 (with who holds it) if it already exists.
  release <path>              remove the lock (idempotent).
  require <path>              exit 4 if no lock exists (no stack is up).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
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


def main() -> int:
    ap = argparse.ArgumentParser(prog="stack_lock")
    ap.add_argument("action", choices=["acquire", "release", "require"])
    ap.add_argument("path", type=Path)
    ap.add_argument("--label", default="integration run")
    args = ap.parse_args()
    if args.action == "acquire":
        return acquire(args.path, args.label)
    if args.action == "release":
        return release(args.path)
    return require(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
