"""CI integration sharding (`scripts/ci_shard.py`, ADR 0015 §24): every test runs in exactly one
shard, and the shards are balanced by measured duration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.ci_shard import DURATIONS, assign, default_seconds, load_durations

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 8])
def test_every_node_id_lands_in_exactly_one_shard(shards: int) -> None:
    nodeids = [f"tests/integration/test_m{m}.py::test_{i}" for m in range(5) for i in range(53)]
    durations = {n: float(hash(n) % 20) for n in nodeids}
    where = assign(nodeids, shards, durations)
    assert set(where) == set(nodeids)  # total: every test assigned
    assert len(where) == len(nodeids)  # exactly one shard each (a dict is single-valued)
    assert set(where.values()) <= set(range(shards))


def test_a_new_test_without_a_recorded_duration_is_still_assigned() -> None:
    nodeids = ["a::x", "b::y", "c::brand_new"]
    where = assign(nodeids, 2, {"a::x": 10.0, "b::y": 10.0})  # c::brand_new unknown
    assert set(where) == set(nodeids) and set(where.values()) <= {0, 1}


def test_assignment_is_deterministic_and_balanced() -> None:
    nodeids = [f"p::t{i}" for i in range(100)]
    durations = {n: float(i % 7 + 1) for i, n in enumerate(nodeids)}
    first = assign(nodeids, 4, durations)
    assert assign(list(reversed(nodeids)), 4, durations) == first  # order-independent
    loads = [0.0, 0.0, 0.0, 0.0]
    for n, g in first.items():
        loads[g] += durations[n]
    assert max(loads) - min(loads) <= max(durations.values())  # within one task of balanced


def test_the_committed_durations_file_is_valid() -> None:
    data = json.loads((REPO / DURATIONS).read_text())
    assert data and all(isinstance(k, str) and isinstance(v, (int, float)) for k, v in data.items())
    assert default_seconds(load_durations()) > 0
