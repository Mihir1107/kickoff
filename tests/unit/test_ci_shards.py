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


def test_verify_uses_the_collected_set_and_places_an_unrecorded_test_in_exactly_one_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 5: `--verify` derives everything from the COLLECTED node ids (a real collection, then one
    per shard with the CI job's environment), not from durations.json. A collected test whose
    duration was never recorded still lands in exactly one shard, and the jobs' selections match
    `assign` (they cut shards from the same post-`-m` set)."""
    from scripts.ci_shard import collect, verify

    collected = collect()
    dropped = collected[len(collected) // 2]
    durations = {k: v for k, v in load_durations().items() if k != dropped}
    durations["tests/integration/test_gone.py::test_deleted_long_ago"] = 9_999.0  # stale entry
    path = tmp_path / "durations.json"
    path.write_text(json.dumps(durations))
    monkeypatch.setenv("EDISC_CI_DURATIONS", str(path))
    result = verify(4)
    assert result.ok, result.problems[:10]
    assert result.collected == collected  # the suite itself, not the durations file's keys
    assert dropped in result.unrecorded
    assert [g for g, ids in enumerate(result.selected) if dropped in ids] != []
    assert sum(ids.count(dropped) for ids in result.selected) == 1
    assert sorted(n for ids in result.selected for n in ids) == sorted(collected)
    assert all("test_gone" not in n for ids in result.selected for n in ids)
