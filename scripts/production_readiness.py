"""Is this tree fit for a production build? Exit 0 when nothing refuses it, 1 otherwise.

    uv run python scripts/production_readiness.py            # make production-readiness

What refuses production today:
- **A break the frontend has not absorbed** (``docs/api/breaking-allowlist.yaml`` entries with
  ``status: pending_frontend``, ``scripts/api_compat.py``): the API no longer offers something the
  frontend still calls. Each such entry names its ``tracking`` requirement; it leaves the list only
  when the frontend absorbs the change.

Two modes (``--mode``):
- ``enforce`` (the default; ``make production-readiness``): exit 1 while anything refuses
  production. Release tags (``v*``) run it in CI, and the ``report`` (production) target of
  ``deploy/report-image/Dockerfile`` runs it in its ``readiness`` stage: a production image cannot be
  built while it fails.
- ``warn`` (CI on main, branches and pull requests): the same findings as a warning annotation
  each and a job summary listing them, exit 0. The job is green, so a red one always means
  something: a malformed allow-list fails in both modes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from scripts import api_compat
except ModuleNotFoundError:  # run as a file (the image's readiness stage): scripts/ is on sys.path
    import api_compat  # type: ignore[no-redef]


def problems(allowlist: Path) -> list[str]:
    entries = api_compat.load_allowlist(allowlist)
    return [
        f"pending_frontend: {e['rule']} {', '.join(api_compat._operations(e))} "
        f"(tracking {e['tracking']})"
        for e in api_compat.pending_frontend(entries)
    ]


def _escape(text: str) -> str:
    """A workflow command's message: GitHub's own escapes for %, CR and LF."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def summary(found: list[str], mode: str) -> str:
    if not found:
        return "### Production readiness\n\nNothing refuses a production build.\n"
    verdict = (
        "**Release blocked.**"
        if mode == "enforce"
        else "Warning only on this ref: release tags and production image builds fail on these."
    )
    items = "".join(f"- `{line}`\n" for line in found)
    return (
        f"### Production readiness: {len(found)} item(s) refuse a production build\n\n"
        f"{verdict}\n\n{items}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="production-readiness gate")
    parser.add_argument("--allowlist", type=Path, default=api_compat.ALLOWLIST)
    parser.add_argument("--mode", choices=("enforce", "warn"), default="enforce")
    args = parser.parse_args(argv)
    found = problems(args.allowlist)  # a malformed list raises: never ready, in either mode
    level = "error" if args.mode == "enforce" else "warning"
    for line in found:
        print(f"NOT READY FOR PRODUCTION: {line}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::{level} title=Not ready for production::{_escape(line)}")
    if not found:
        print("production-readiness: nothing refuses a production build")
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(summary(found, args.mode))
    return 1 if found and args.mode == "enforce" else 0


if __name__ == "__main__":
    sys.exit(main())
