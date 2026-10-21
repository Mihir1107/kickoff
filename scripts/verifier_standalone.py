"""The standalone verifier distribution (M18 step 5, plan §5, D12): its hashed requirements, its
reproducible wheel, and an install into an EMPTY virtual environment.

  uv run python scripts/verifier_standalone.py lock          # write packages/verify/requirements.lock
  uv run python scripts/verifier_standalone.py check-lock    # fail if it differs from uv.lock
  uv run python scripts/verifier_standalone.py build --out DIR   # build twice, compare, print SHA-256
  uv run python scripts/verifier_standalone.py venv --out DIR    # build + an empty venv with it

`build`: the wheel is built twice with ``SOURCE_DATE_EPOCH`` = the commit time (hatchling's
reproducible mode then stamps every entry with it); the two must be byte-identical and every entry
must carry exactly that timestamp. `venv`: ``python -m venv`` (nothing but pip), the pinned
dependencies downloaded once with ``--require-hashes``, then installed ``--no-index`` from that
directory with ``--require-hashes``, then the wheel ``--no-index --no-deps``; the venv must then
hold exactly the wheel, its locked dependencies and pip. Prints the venv's ``edisc-verify`` path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "verify"
LOCK = PACKAGE / "requirements.lock"
LOCK_HEADER = (
    "# The standalone verifier's dependencies, pinned with hashes (generated from uv.lock by\n"
    "# scripts/verifier_standalone.py lock; a unit test fails when it drifts). Install with\n"
    "#   pip install --require-hashes -r requirements.lock\n"
)


def _run(*argv: str | Path, env: dict[str, str] | None = None, cwd: Path = ROOT) -> str:
    proc = subprocess.run(  # noqa: S603  (an argv list, never a shell)
        [str(a) for a in argv], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise SystemExit(f"{' '.join(map(str, argv))} failed ({proc.returncode}):\n{proc.stderr}")
    return proc.stdout


def locked_requirements() -> str:
    exported = _run(
        "uv", "export", "--package", "edisc-verify", "--no-dev", "--no-emit-workspace",
        "--frozen", "--format", "requirements-txt", "--no-header",
    )  # fmt: skip
    return LOCK_HEADER + exported


def commit_epoch() -> int:
    return int(_run("git", "log", "-1", "--format=%ct").strip())


def build_once(out: Path, epoch: int) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
    _run("uv", "build", "--package", "edisc-verify", "--wheel", "--out-dir", out, env=env)
    (wheel,) = sorted(out.glob("edisc_verify-*.whl"))
    return wheel


def check_timestamps(wheel: Path, epoch: int) -> None:
    """Every entry stamped with the commit time (zip time has 2-second resolution)."""
    stamp = datetime.fromtimestamp(epoch - epoch % 2, UTC).timetuple()[:6]
    with zipfile.ZipFile(wheel) as zf:
        odd = sorted({i.date_time for i in zf.infolist()} - {stamp})
    if odd:
        raise SystemExit(f"{wheel.name}: entries not stamped with the commit time {stamp}: {odd}")


def build(out: Path) -> tuple[Path, str]:
    epoch = commit_epoch()
    first, second = build_once(out / "a", epoch), build_once(out / "b", epoch)
    digests = {hashlib.sha256(w.read_bytes()).hexdigest() for w in (first, second)}
    if len(digests) != 1:
        raise SystemExit(f"the verifier wheel is not reproducible: {sorted(digests)}")
    check_timestamps(first, epoch)
    return first, digests.pop()


def venv(out: Path, wheel: Path) -> Path:
    env_dir, wheels = out / "venv", out / "wheels"
    _run(sys.executable, "-m", "venv", env_dir)  # empty: pip only, no system site-packages
    pip = env_dir / "bin" / "pip"
    _run(pip, "download", "--require-hashes", "--only-binary=:all:", "-r", LOCK, "-d", wheels)
    _run(pip, "install", "--no-index", "--find-links", wheels, "--require-hashes", "-r", LOCK)
    _run(pip, "install", "--no-index", "--no-deps", wheel)
    installed = {d["name"].lower() for d in json.loads(_run(pip, "list", "--format=json"))}
    expected = {
        line.split("==")[0].lower()
        for line in LOCK.read_text().splitlines()
        if "==" in line and not line.startswith("#")
    } | {"edisc-verify", "pip"}
    if installed != expected:
        raise SystemExit(f"the venv holds {sorted(installed)}, expected {sorted(expected)}")
    exe = env_dir / "bin" / "edisc-verify"
    _run(exe, "--help")
    return exe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("lock", "check-lock", "build", "venv"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.action == "lock":
        LOCK.write_text(locked_requirements())
    elif args.action == "check-lock":
        if LOCK.read_text() != locked_requirements():
            raise SystemExit(f"{LOCK} differs from uv.lock: run this script with `lock`")
    else:
        if args.out is None:
            parser.error("--out is required")
        wheel, digest = build(args.out)
        sys.stdout.write(f"{wheel.name} sha256 {digest}\n")
        if args.action == "venv":
            sys.stdout.write(f"{venv(args.out, wheel)}\n")


if __name__ == "__main__":
    main()
