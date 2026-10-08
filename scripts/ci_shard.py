"""Balanced sharding of the integration suite across parallel CI jobs (ADR 0015 §24).

The suite takes ~22 min of test time on a hosted runner (plus ~2-3 min to bring the stack up and
tear it down). Splitting it into N shards -- one CI job, one ephemeral stack each -- brings wall time
per shard well under 20 min, with real headroom under the 45-minute job limit. Shards are balanced by
MEASURED per-test duration (`tests/integration/durations.json`, call+setup+teardown summed), assigned
longest-first to the currently-lightest shard (LPT), which keeps the heaviest tests (the 50k soak, the
batch-boundary renders) on different shards.

Every collected test lands in exactly ONE shard: `assign` is a total function over the collected node
ids, and `tests/integration/conftest.py` deselects the tests not in this job's shard
(`EDISC_CI_SHARD` / `EDISC_CI_SHARDS`). `--verify` collects the real suite and asserts the shards are
a disjoint cover; `tests/unit/test_ci_shards.py` checks `assign` directly.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
DURATIONS = _REPO / "tests" / "integration" / "durations.json"


def load_durations(path: Path = DURATIONS) -> dict[str, float]:
    try:
        raw: dict[str, float] = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw


def default_seconds(durations: Mapping[str, float]) -> float:
    """What to assume for a test with no recorded duration (new tests): the median, so one unknown
    test never dominates or disappears in the balance."""
    return statistics.median(durations.values()) if durations else 1.0


def assign(nodeids: Sequence[str], shards: int, durations: Mapping[str, float]) -> dict[str, int]:
    """Map each node id to a shard index in [0, shards) by longest-processing-time greedy packing.
    Deterministic: ties break by node id. Every node id appears exactly once in the result."""
    if shards < 1:
        raise ValueError("shards must be >= 1")
    default = default_seconds(durations)
    order = sorted(nodeids, key=lambda n: (-durations.get(n, default), n))
    load = [0.0] * shards
    out: dict[str, int] = {}
    for nodeid in order:
        g = min(range(shards), key=lambda i: (load[i], i))  # lightest shard, ties by index
        out[nodeid] = g
        load[g] += durations.get(nodeid, default)
    return out


def collect(target: str = "tests/integration") -> list[str]:
    """The node ids pytest collects for the integration suite, in collection order (no services
    needed: collection only imports)."""
    proc = subprocess.run(  # noqa: S603  (fixed internal argv, our own interpreter)
        [sys.executable, "-m", "pytest", target, "-m", "not elasticsearch",
         "--collect-only", "-q", "-p", "no:randomly"],
        cwd=_REPO, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return [line for line in proc.stdout.splitlines() if "::" in line]


def _verify(shards: int) -> int:
    nodeids = collect()
    durations = load_durations()
    out = assign(nodeids, shards, durations)
    groups: list[list[str]] = [[] for _ in range(shards)]
    for nodeid, g in out.items():
        groups[g].append(nodeid)
    covered = {n for grp in groups for n in grp}
    ok = covered == set(nodeids) and sum(len(g) for g in groups) == len(nodeids) == len(
        set(nodeids)
    )
    default = default_seconds(durations)
    for i, grp in enumerate(groups):
        secs = sum(durations.get(n, default) for n in grp)
        print(f"  shard {i}: {len(grp):4d} tests, ~{secs:6.0f}s")
    if not ok:
        print("FAIL: shards are not a disjoint cover of the collected tests", file=sys.stderr)
        return 1
    print(f"OK: {len(nodeids)} tests across {shards} shards, each run exactly once")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ci_shard")
    ap.add_argument("--of", type=int, required=True, help="number of shards")
    ap.add_argument("--shard", type=int, help="print the node ids for this shard index")
    ap.add_argument("--verify", action="store_true", help="assert the shards are a disjoint cover")
    args = ap.parse_args()
    if args.verify:
        return _verify(args.of)
    if args.shard is None:
        ap.error("pass --shard G or --verify")
    out = assign(collect(), args.of, load_durations())
    for nodeid in collect():
        if out[nodeid] == args.shard:
            print(nodeid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
