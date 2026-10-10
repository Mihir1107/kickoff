"""Balanced sharding of the integration suite across parallel CI jobs (ADR 0015 §24).

The suite takes ~22 min of test time on a hosted runner (plus ~2-3 min to bring the stack up and
tear it down). Splitting it into N shards -- one CI job, one ephemeral stack each -- brings wall time
per shard well under 20 min, with real headroom under the 45-minute job limit. Shards are balanced by
MEASURED per-test duration (`tests/integration/durations.json`, call+setup+teardown summed), assigned
longest-first to the currently-lightest shard (LPT), which keeps the heaviest tests (the 50k soak, the
batch-boundary renders) on different shards.

Every collected test lands in exactly ONE shard: `assign` is a total function over the collected node
ids (a test missing from `durations.json`, e.g. a new one, is assigned with the median duration), and
`tests/integration/conftest.py` deselects the tests not in this job's shard (`EDISC_CI_SHARD` /
`EDISC_CI_SHARDS`), AFTER `-m` deselection. `--verify` is computed from the COLLECTED test set, never
from `durations.json`: it collects the suite as the CI job does, then collects it once more per shard
with the job's environment, and asserts the shards the jobs will actually select are pairwise
disjoint, cover the collected set exactly, and match `assign`. `tests/unit/test_ci_shards.py` checks
`assign` and `verify` (with a durations file that lacks a collected test).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
DURATIONS = _REPO / "tests" / "integration" / "durations.json"
# the CI job's selection (Makefile `test-integration`): keep in sync
MARKERS = "not elasticsearch"


def load_durations(path: Path | None = None) -> dict[str, float]:
    """``path``, else `EDISC_CI_DURATIONS` (tests), else the committed file."""
    path = path or Path(os.environ.get("EDISC_CI_DURATIONS") or DURATIONS)
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


def collect(target: str = "tests/integration", env: Mapping[str, str] | None = None) -> list[str]:
    """The node ids pytest SELECTS for the integration suite with the CI job's marker expression,
    in collection order (no services needed: collection only imports). ``env`` adds variables
    (e.g. a shard's `EDISC_CI_SHARDS` / `EDISC_CI_SHARD`)."""
    run_env = {
        k: v for k, v in os.environ.items() if k not in ("EDISC_CI_SHARDS", "EDISC_CI_SHARD")
    }
    proc = subprocess.run(  # noqa: S603  (fixed internal argv, our own interpreter)
        [sys.executable, "-m", "pytest", target, "-m", MARKERS,
         "--collect-only", "-q", "-p", "no:randomly"],
        cwd=_REPO, capture_output=True, text=True, check=True, env={**run_env, **(env or {})},
    )  # fmt: skip
    return [line for line in proc.stdout.splitlines() if "::" in line]


@dataclass
class Verified:
    collected: list[str]
    selected: list[list[str]]  # what each shard's CI job selects (a real collection per shard)
    unrecorded: list[str]  # collected tests missing from durations.json (assigned the median)
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(shards: int, target: str = "tests/integration") -> Verified:
    """Collect the suite as the CI job does, then once per shard with the job's environment, and
    check the shards are a disjoint cover of the collected set that matches `assign`. Everything
    is derived from the collected node ids; `durations.json` only weighs them."""
    collected = collect(target)
    durations = load_durations()
    predicted = assign(collected, shards, durations)
    selected = [
        collect(target, {"EDISC_CI_SHARDS": str(shards), "EDISC_CI_SHARD": str(g)})
        for g in range(shards)
    ]
    problems: list[str] = []
    if len(set(collected)) != len(collected):
        problems.append("duplicate node ids in the collection")
    seen: dict[str, int] = {}
    for g, ids in enumerate(selected):
        for nodeid in ids:
            if nodeid in seen:
                problems.append(f"{nodeid} selected by shards {seen[nodeid]} and {g}")
            seen[nodeid] = g
            if predicted.get(nodeid) != g:
                problems.append(
                    f"{nodeid}: shard {g} selected it, assign says {predicted.get(nodeid)}"
                )
    missing = set(collected) - set(seen)
    extra = set(seen) - set(collected)
    problems += [f"{n} is in no shard" for n in sorted(missing)]
    problems += [f"{n} is selected but not collected" for n in sorted(extra)]
    unrecorded = [n for n in collected if n not in durations]
    return Verified(collected, selected, unrecorded, problems)


def _verify(shards: int) -> int:
    result = verify(shards)
    durations = load_durations()
    default = default_seconds(durations)
    for i, grp in enumerate(result.selected):
        secs = sum(durations.get(n, default) for n in grp)
        print(f"  shard {i}: {len(grp):4d} tests selected by its job, ~{secs:6.0f}s")
    print(f"  {len(result.unrecorded)} collected tests have no recorded duration (median assumed)")
    if not result.ok:
        for problem in result.problems[:50]:
            print(f"FAIL: {problem}", file=sys.stderr)
        print("FAIL: shards are not a disjoint cover of the collected tests", file=sys.stderr)
        return 1
    total = sum(len(g) for g in result.selected)
    print(f"OK: {len(result.collected)} collected tests across {shards} shards ({total} selected),"
          " each run exactly once")  # fmt: skip
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
    nodeids = collect()
    out = assign(nodeids, args.of, load_durations())
    for nodeid in nodeids:
        if out[nodeid] == args.shard:
            print(nodeid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
