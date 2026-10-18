"""Phase timings of a slow test, with what the host did meanwhile (ADR 0015 §24.20).

A test that runs out of its bound on some CI runners only says "too slow". This records, per phase:
wall time, the test process's own CPU time (the in-process API and worker included), the host's
CPU split from ``/proc/stat`` (user, system, iowait, steal; Linux only) and the load average, plus a
fixed CPU calibration before and after. From those a run tells the two causes apart: if every phase
scales with the calibration, the runner is slower at pure CPU work; if one phase grows while the
calibration does not, that phase met something (a disk, a lock, a service) and is a finding.

The record goes to ``$EDISC_TEST_PHASE_TIMINGS_DIR/<test>.json`` and, on GitHub, a table in the job
summary (``$GITHUB_STEP_SUMMARY``). It is written whether the test passes or fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import resource
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

_STAT_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def _host_cpu() -> dict[str, int] | None:
    """The host's aggregate CPU jiffies, or None where /proc/stat does not exist (macOS)."""
    try:
        first = Path("/proc/stat").read_text().splitlines()[0].split()
    except OSError:
        return None
    return dict(zip(_STAT_FIELDS, (int(v) for v in first[1:9]), strict=False))


def _process_cpu_s() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _calibrate() -> float:
    """A fixed CPU-only workload (hashing and the interpreter's own loop), in seconds. Run in a
    thread: it must not block the test's event loop (the loop guard)."""
    started = time.perf_counter()
    h = hashlib.sha256()
    block = b"\x5a" * (1 << 20)
    for _ in range(64):
        h.update(block)
    total = 0
    for i in range(2_000_000):
        total += i * i % 7
    json.loads(json.dumps([{"k": i, "v": str(i)} for i in range(50_000)]))
    return time.perf_counter() - started


Probe = Callable[[], Awaitable[dict[str, int]]]


class PhaseClock:
    def __init__(self, test: str, probe: Probe | None = None) -> None:
        """``probe``: counters read before and after each phase (e.g. the database's WAL
        statistics); the phase records their differences."""
        self.test = test
        self.probe = probe
        self.phases: list[dict[str, Any]] = []
        self.extra: dict[str, Any] = {}
        self.outcome = "unfinished"

    @contextlib.asynccontextmanager
    async def phase(self, name: str) -> AsyncIterator[None]:
        counters0 = await self.probe() if self.probe is not None else None
        cpu0, host0, wall0 = _process_cpu_s(), _host_cpu(), time.monotonic()
        try:
            yield
        finally:
            wall = time.monotonic() - wall0
            row: dict[str, Any] = {
                "phase": name,
                "wall_s": round(wall, 3),
                "process_cpu_s": round(_process_cpu_s() - cpu0, 3),
                "load_avg_1m": round(os.getloadavg()[0], 2),
            }
            host1 = _host_cpu()
            if host0 is not None and host1 is not None:
                delta = {k: host1[k] - host0[k] for k in _STAT_FIELDS}
                total = sum(delta.values()) or 1
                row["host_pct"] = {k: round(100 * v / total, 1) for k, v in delta.items() if v}
            if counters0 is not None and self.probe is not None:
                counters1 = await self.probe()
                row["counters"] = {k: counters1[k] - counters0[k] for k in counters0}
            self.phases.append(row)

    async def calibrate(self, label: str) -> None:
        self.extra[f"calibration_{label}_s"] = round(await asyncio.to_thread(_calibrate), 3)

    def record(self) -> dict[str, Any]:
        return {
            "test": self.test,
            "outcome": self.outcome,
            "host": {"cpus": os.cpu_count(), "machine": platform.machine(),
                     "runner": os.environ.get("RUNNER_NAME"),
                     "run": os.environ.get("GITHUB_RUN_ID"),
                     "sha": os.environ.get("GITHUB_SHA")},
            "phases": self.phases,
            **self.extra,
        }  # fmt: skip

    def write(self) -> None:
        record = self.record()
        target = os.environ.get("EDISC_TEST_PHASE_TIMINGS_DIR")
        if target:
            Path(target).mkdir(parents=True, exist_ok=True)
            name = self.test.replace("/", "_").replace(":", "_")
            (Path(target) / f"{name}.json").write_text(json.dumps(record, indent=1) + "\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            lines = [
                f"### Phase timings: `{self.test}` ({self.outcome})\n",
                "| phase | wall s | process CPU s | load 1m | host CPU % | counters |",
                "|---|---|---|---|---|---|",
                *(
                    f"| {p['phase']} | {p['wall_s']} | {p['process_cpu_s']} | {p['load_avg_1m']} "
                    f"| {p.get('host_pct', '')} | {p.get('counters', '')} |"
                    for p in self.phases
                ),
                "",
                *(f"- {k}: {v}" for k, v in self.extra.items() if k != "worker_phases"),
                f"- worker phases: `{json.dumps(self.extra.get('worker_phases'))}`",
                "",
            ]
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")


@contextlib.asynccontextmanager
async def phase_clock(test: str, probe: Probe | None = None) -> AsyncIterator[PhaseClock]:
    clock = PhaseClock(test, probe)
    await clock.calibrate("before")
    try:
        yield clock
        clock.outcome = "passed"
    except BaseException:
        clock.outcome = "failed"
        raise
    finally:
        await clock.calibrate("after")
        clock.write()
