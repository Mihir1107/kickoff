"""M8: 10 separate worker processes sharing one Redis bucket."""

from __future__ import annotations

import asyncio
import bisect
import json
import os
import subprocess
import sys
import uuid
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WORKERS = 10
RATE, BURST = 40.0, 4
# the stack under test: the ephemeral edisc-test project under `make test-integration`
COMPOSE = [
    "docker",
    "compose",
    "-p",
    os.environ.get("EDISC_COMPOSE_PROJECT", "edisc"),
    "-f",
    str(ROOT / "infra/docker-compose.yml"),
    "--env-file",
    str(ROOT / os.environ.get("EDISC_ENV_FILE", ".env")),
]


async def run_workers(
    tmp: Path, *, tenant: uuid.UUID, extra: dict[int, list[str]] | None = None, common: list[str]
) -> list[asyncio.subprocess.Process]:
    procs = []
    for wid in range(WORKERS):
        args = [
            sys.executable,
            "-m",
            "tests.integration.ratelimit.worker",
            "--tenant",
            str(tenant),
            "--wid",
            str(wid),
            "--rate",
            str(RATE),
            "--burst",
            str(BURST),
            "--out",
            str(tmp / f"w{wid}.jsonl"),
            *common,
            *((extra or {}).get(wid, [])),
        ]
        procs.append(
            await asyncio.create_subprocess_exec(*args, cwd=ROOT, stderr=asyncio.subprocess.PIPE)
        )
    return procs


async def wait_all(procs: list[asyncio.subprocess.Process]) -> None:
    for p in procs:
        _, err = await asyncio.wait_for(p.communicate(), timeout=90)
        assert p.returncode == 0, err.decode()[-2000:]


def records(tmp: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for f in sorted(tmp.glob("w*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def assert_never_exceeds(grant_times_us: list[int]) -> None:
    """For EVERY window [t_i, t_j] of grants: count <= burst + rate * (t_j - t_i). Server time only."""
    times = sorted(grant_times_us)
    assert times, "no grants"
    for i, start in enumerate(times):
        for j in range(i, len(times)):
            allowed = BURST + RATE * (times[j] - start) / 1_000_000 + 1e-6
            assert j - i + 1 <= allowed, (
                f"{j - i + 1} grants in {(times[j] - start) / 1e6:.3f}s (allowed {allowed:.2f})"
            )


def grants(recs: list[dict[str, object]]) -> list[int]:
    return [int(r["t"]) for r in recs if r["event"] == "grant"]  # type: ignore[call-overload]


async def test_ten_processes_never_exceed_the_rate(tmp_path: Path) -> None:
    await wait_all(await run_workers(tmp_path, tenant=uuid.uuid4(), common=["--duration", "6"]))
    times = grants(records(tmp_path))
    assert_never_exceeds(times)
    span = (max(times) - min(times)) / 1e6
    assert len(times) >= 0.85 * RATE * span, (
        f"limiter under-delivers: {len(times)} grants in {span:.1f}s"
    )
    assert (
        len({r["w"] for r in records(tmp_path) if r["event"] == "grant"}) == WORKERS
    )  # everyone got work


async def test_skewed_worker_clocks_do_not_change_the_rate(tmp_path: Path) -> None:
    skews = [-86400, -3600, -60, -1, 0, 0.5, 5, 90, 3600, 86400]
    extra = {w: ["--skew", str(s)] for w, s in enumerate(skews)}
    await wait_all(
        await run_workers(tmp_path, tenant=uuid.uuid4(), extra=extra, common=["--duration", "5"])
    )
    times = grants(records(tmp_path))
    assert_never_exceeds(times)
    span = (max(times) - min(times)) / 1e6
    assert len(times) >= 0.85 * RATE * span


async def test_one_workers_429_pauses_all_ten(tmp_path: Path) -> None:
    retry_after = 1.5
    # early enough that worker 3 always reaches it (10 workers share 40 req/s: ~24 each in 6 s)
    extra = {3: ["--pause-at", "5", "--retry-after", str(retry_after)]}
    await wait_all(
        await run_workers(tmp_path, tenant=uuid.uuid4(), extra=extra, common=["--duration", "6"])
    )
    recs = records(tmp_path)
    pauses = [r for r in recs if r["event"] == "pause"]
    assert len(pauses) == 1, "the injected 429 did not fire"
    until = int(pauses[0]["until"])  # type: ignore[call-overload]
    started = until - int(retry_after * 1_000_000)  # server time when the pause was published
    times = sorted(grants(recs))
    inside = [t for t in times if started <= t < until]
    assert inside == [], f"{len(inside)} grants during the {retry_after}s pause"
    after = times[bisect.bisect_left(times, until) :]
    assert after, "work never resumed"
    assert (after[0] - until) / 1e6 < 0.5  # resumes promptly (bucket drained, refills at the rate)
    workers_after = {r["w"] for r in recs if r["event"] == "grant" and int(r["t"]) >= until}  # type: ignore[call-overload]
    assert len(workers_after) == WORKERS  # ALL workers were held and all resumed
    assert_never_exceeds(times)


def _compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True, timeout=120)


@pytest.mark.slow
async def test_redis_restart_pauses_without_burst_or_lost_units(tmp_path: Path) -> None:
    units = 30
    procs = await run_workers(tmp_path, tenant=uuid.uuid4(), common=["--units", str(units)])
    try:
        await asyncio.sleep(2.0)
        await asyncio.to_thread(_compose, "stop", "redis")
        await asyncio.sleep(3.0)
    finally:
        await asyncio.to_thread(_compose, "up", "-d", "--wait", "redis")
    await wait_all(procs)

    recs = records(tmp_path)
    for wid in range(WORKERS):
        done = [r["unit"] for r in recs if r["w"] == wid and r["event"] == "done"]
        assert done == list(range(units)), f"worker {wid} skipped or repeated units: {done}"
    unavailable = {r["w"] for r in recs if r["event"] == "unavailable"}
    assert unavailable == set(range(WORKERS)), "every worker must have paused while Redis was down"
    times = sorted(grants(recs))
    assert_never_exceeds(times)
    gaps = [(b - a) / 1e6 for a, b in pairwise(times)]
    assert max(gaps) >= 2.5, "there must be a gap while Redis was down (no unthrottled fetching)"
