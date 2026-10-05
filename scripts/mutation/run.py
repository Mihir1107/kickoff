"""Run the mutation catalog (see README.md): break one protection at a time, run the test that guards
it, require a failure, restore the file from a copy (never ``git checkout``: other work in the tree
must survive).

    uv run python scripts/mutation/run.py --list
    uv run python scripts/mutation/run.py --kind unit
    uv run python scripts/mutation/run.py --kind integration      # needs `make test-env-up` first
    uv run python scripts/mutation/run.py --round s21-review --only native_copy_unlocked
    uv run python scripts/mutation/run.py --check                  # every edit still applies

Exit 0 only if every selected mutation was caught (its test failed) and every edit applied.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog import CATALOG, Mutation  # noqa: E402


def apply(text: str, mutation: Mutation) -> str:
    for old, new in mutation.edits:
        count = text.count(old)
        if count != 1:
            raise LookupError(f"{mutation.name}: edit found {count} times in {mutation.path}")
        text = text.replace(old, new)
    return text


def command(mutation: Mutation) -> list[str]:
    select = ["-k", mutation.select] if mutation.select else []
    if mutation.kind == "unit":
        return ["uv", "run", "pytest", "-q", "-x", "-p", "no:cacheprovider", mutation.test, *select]
    quoted = f" -k '{mutation.select}'" if mutation.select else ""
    return ["make", "test-integration-only", f"TESTS={mutation.test} -q -x{quoted}"]


def run_one(mutation: Mutation, verbose: bool) -> str:
    path = ROOT / mutation.path
    backup = path.with_name(path.name + ".mutation-backup")
    if backup.exists():
        raise SystemExit(
            f"{backup} exists: an earlier run was interrupted; restore it by hand first"
        )
    original = path.read_text()
    mutated = apply(original, mutation)
    shutil.copy2(path, backup)
    try:
        path.write_text(mutated)
        started = time.monotonic()
        # a fresh bytecode cache per run: two same-sized edits of one file within one second would
        # otherwise pass Python's mtime+size check and load the previous run's stale .pyc
        with tempfile.TemporaryDirectory(prefix="mutation-pyc-") as pyc:
            proc = subprocess.run(  # noqa: S603 - fixed argv from the catalog, no user input
                command(mutation), cwd=ROOT, capture_output=True, text=True, timeout=1800,
                env={**os.environ, "PYTHONPYCACHEPREFIX": pyc},
            )  # fmt: skip
        took = time.monotonic() - started
        if verbose:
            sys.stdout.write(proc.stdout[-3000:] + proc.stderr[-2000:])
        return f"{'caught' if proc.returncode != 0 else 'NOT CAUGHT'} ({took:.0f}s)"
    finally:
        shutil.copy2(backup, path)
        backup.unlink()
        if path.read_text() != original:
            raise SystemExit(f"{path} was not restored")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--kind", choices=["unit", "integration", "all"], default="all")
    parser.add_argument(
        "--round", action="append", default=[], help="repeatable; default: all rounds"
    )
    parser.add_argument("--only", action="append", default=[], help="mutation name; repeatable")
    parser.add_argument("--list", action="store_true", help="list the selection and exit")
    parser.add_argument("--check", action="store_true", help="only check that every edit applies")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print each test run's output tail"
    )
    args = parser.parse_args()

    selected = [
        mut for mut in CATALOG
        if (args.kind == "all" or mut.kind == args.kind)
        and (not args.round or mut.round in args.round)
        and (not args.only or mut.name in args.only)
    ]  # fmt: skip
    if args.list:
        for mut in selected:
            print(f"{mut.round:12} {mut.kind:11} {mut.name:34} {mut.test} -k {mut.select!r}")
        print(f"{len(selected)} mutations")
        return 0
    if args.check:
        bad = []
        for mut in selected:
            try:
                apply((ROOT / mut.path).read_text(), mut)
            except LookupError as exc:
                bad.append(str(exc))
        print("\n".join(bad) or f"all {len(selected)} edits apply")
        return 1 if bad else 0

    # restore the file being mutated on Ctrl-C too (the finally block runs on KeyboardInterrupt)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    problems = []
    for mut in selected:
        try:
            outcome = run_one(mut, args.verbose)
        except LookupError as exc:
            outcome = f"EDIT DOES NOT APPLY: {exc}"
        print(f"{outcome:22} {mut.round:12} {mut.name}", flush=True)
        if not outcome.startswith("caught"):
            problems.append(mut.name)
    print(f"{len(selected) - len(problems)}/{len(selected)} caught; problems: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
