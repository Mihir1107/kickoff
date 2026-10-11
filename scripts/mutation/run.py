"""Run the mutation catalog (see README.md): break one protection at a time, run the test that guards
it, require a failure, restore the file from a copy (never ``git checkout``: other work in the tree
must survive).

    uv run python scripts/mutation/run.py --list
    uv run python scripts/mutation/run.py --kind unit
    uv run python scripts/mutation/run.py --kind integration      # needs `make test-env-up` first
    uv run python scripts/mutation/run.py --round s21-review --only native_copy_unlocked
    uv run python scripts/mutation/run.py --check                  # every edit still applies
    python scripts/mutation/run.py --kind image                     # INSIDE the report-test image

Exit 0 only if every selected mutation was caught (its test failed) and every edit applied.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# the report image (deploy/report-image/Dockerfile): the code is installed NON-editable into two
# venvs (the worker's, which the PDF child runs, and the tests'), so an image break edits the
# installed copies; the repo copy (under /src) is only checked to apply the same way
IMAGE_VENVS = (Path("/opt/edisc/venv"), Path("/opt/edisc/testenv"))
IMAGE_FILES = {"deploy/report-image/fonts.conf": Path("/opt/edisc/fonts.conf")}


def in_image() -> bool:
    return all(v.is_dir() for v in IMAGE_VENVS)


def targets(mutation: Mutation) -> list[Path]:
    """The files a break edits: the repo file on the host; inside the report image, the installed
    copies (``packages/<p>/src/<mod>/...`` -> every venv's ``site-packages/<mod>/...``)."""
    if mutation.kind != "image":
        return [ROOT / mutation.path]
    if mutation.path in IMAGE_FILES:
        return [IMAGE_FILES[mutation.path]]
    _, sep, module_path = mutation.path.partition("/src/")
    if not sep:
        raise LookupError(f"{mutation.name}: no installed copy of {mutation.path}")
    found = [p for v in IMAGE_VENVS for p in v.glob(f"lib/python3*/site-packages/{module_path}")]
    if not found:
        raise LookupError(f"{mutation.name}: {module_path} is not installed in the image")
    return found


sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog import CATALOG, Mutation  # noqa: E402

RAN = re.compile(r"\b\d+ (passed|failed|errors?)\b")


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
    if mutation.kind == "image":  # the test venv's python, inside the image
        return ["python", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "no:randomly",
                mutation.test, *select]  # fmt: skip
    quoted = f" -k '{mutation.select}'" if mutation.select else ""
    markers = (  # tests with their own CI job are deselected from the default suite
        ["INTEGRATION_MARKERS=production_install"]
        if mutation.test.startswith("tests/integration/production/")
        else []
    )
    return ["make", "test-integration-only", f"TESTS={mutation.test} -q -x{quoted}", *markers]


def run_one(mutation: Mutation, verbose: bool) -> str:
    paths = targets(mutation)
    backups = [p.with_name(p.name + ".mutation-backup") for p in paths]
    for backup in backups:
        if backup.exists():
            raise SystemExit(
                f"{backup} exists: an earlier run was interrupted; restore it by hand first"
            )
    originals = [p.read_text() for p in paths]
    mutated = [apply(o, mutation) for o in originals]
    for p, b in zip(paths, backups, strict=True):
        shutil.copy2(p, b)
    try:
        for p, text in zip(paths, mutated, strict=True):
            p.write_text(text)
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
        if not RAN.search(proc.stdout):  # an empty selection makes pytest fail: never "caught"
            return f"NO TEST RAN ({took:.0f}s)"
        return f"{'caught' if proc.returncode != 0 else 'NOT CAUGHT'} ({took:.0f}s)"
    finally:
        for p, b, original in zip(paths, backups, originals, strict=True):
            shutil.copy2(b, p)
            b.unlink()
            if p.read_text() != original:
                raise SystemExit(f"{p} was not restored")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--kind", choices=["unit", "integration", "image", "all"], default="all",
        help="all = unit + integration (host); image runs only inside the report-test image",
    )  # fmt: skip
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
        if (mut.kind == args.kind or (args.kind == "all" and mut.kind != "image"))
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

    if any(m.kind == "image" for m in selected) and not in_image():
        print("image breaks run inside the report-test image (make test-report-pdf)")
        return 2
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
