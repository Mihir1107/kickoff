"""Is this tree fit for a production build? Exit 0 when nothing refuses it, 1 otherwise.

    uv run python scripts/production_readiness.py            # make production-readiness

What refuses production today:
- **A break the frontend has not absorbed** (``docs/api/breaking-allowlist.yaml`` entries with
  ``status: pending_frontend``, ``scripts/api_compat.py``): the API no longer offers something the
  frontend still calls. Each such entry names its ``tracking`` requirement; it leaves the list only
  when the frontend absorbs the change.

Where it runs: the CI job ``production-readiness`` (failing, and blocking on release tags), and the
``report`` (production) target of ``deploy/report-image/Dockerfile``, whose ``readiness`` stage runs
this script: a production image cannot be built while it fails.
"""

from __future__ import annotations

import argparse
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="production-readiness gate")
    parser.add_argument("--allowlist", type=Path, default=api_compat.ALLOWLIST)
    args = parser.parse_args(argv)
    found = problems(args.allowlist)
    for line in found:
        print(f"NOT READY FOR PRODUCTION: {line}")
    if not found:
        print("production-readiness: nothing refuses a production build")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
