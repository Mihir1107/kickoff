"""What the database did during a slow phase (ADR 0015 §24.20): wait events and the plans actually used.

``WaitSampler`` polls ``pg_stat_activity`` every ``interval`` seconds from its own superuser
connection and records, per backend kind and query, how often it was seen on CPU or waiting on
what, with autovacuum / autoanalyze workers named by their relation and timed (start and end, on
the same monotonic clock as ``ExportIngest.series``).

``AutoExplain`` turns ``auto_explain`` on for the whole cluster for the duration of the block
(``ALTER SYSTEM`` + reload, reset after), at ``NOTICE`` level, so every backend that runs a sampled
statement sends its plans to its own client. A log listener on every connection of the given
engine (the one the in-process worker uses) collects them: the plans those backends EXECUTED, at
the time they ran, including the nested statements (foreign-key checks run as SPI queries with
plans cached per backend), never a plan re-derived afterwards. ``auto_explain`` must be preloaded
(``infra/docker-compose.yml``); it is inert until enabled here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable
from typing import Any

import asyncpg
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

ACTIVITY = """
SELECT a.backend_type, a.state, a.wait_event_type, a.wait_event, left(a.query, 200) AS query
FROM pg_stat_activity a
WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
  AND (a.state IS DISTINCT FROM 'idle' OR a.backend_type <> 'client backend')
"""

_SPACES = re.compile(r"\s+")


def _query_head(query: str) -> str:
    return _SPACES.sub(" ", query).strip()[:70]


class WaitSampler:
    def __init__(self, connect: Callable[[], Any], interval: float = 0.1) -> None:
        self.connect, self.interval = connect, interval
        self.samples = 0
        # (backend kind, query head) -> wait ("CPU" when active and not waiting) -> samples
        self.waits: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        # autovacuum / autoanalyze activity: query -> [first seen, last seen] (monotonic s)
        self.maintenance: dict[str, list[float]] = {}
        self.slowest_sample_ms = 0.0

    async def _run(self, conn: asyncpg.Connection) -> None:
        while True:
            t0 = time.monotonic()
            rows = await conn.fetch(ACTIVITY)
            t1 = time.monotonic()
            self.samples += 1
            self.slowest_sample_ms = max(self.slowest_sample_ms, (t1 - t0) * 1000)
            for r in rows:
                kind = r["backend_type"]
                query = _query_head(r["query"] or "")
                if kind == "autovacuum worker":
                    span = self.maintenance.setdefault(query, [t0, t0])
                    span[1] = t0
                if kind not in ("client backend", "autovacuum worker"):
                    continue  # walwriter, checkpointer, ... (their waits are idle by design)
                wait = (
                    f"{r['wait_event_type']}:{r['wait_event']}"
                    if r["wait_event_type"]
                    else ("CPU" if r["state"] in ("active", None) else str(r["state"]))
                )
                self.waits[(kind, query)][wait] += 1
            await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - t0)))

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[WaitSampler]:
        conn = await self.connect()
        task = asyncio.create_task(self._run(conn))
        try:
            yield self
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await conn.close()

    def record(self, zero: float) -> dict[str, Any]:
        """Times relative to ``zero`` (the monotonic instant the series start from), in ms."""
        return {
            "samples": self.samples,
            "slowest_sample_ms": round(self.slowest_sample_ms, 1),
            "waits": [
                {"backend": k[0], "query": k[1], "waits": dict(c.most_common())}
                for k, c in sorted(self.waits.items(), key=lambda kv: -sum(kv[1].values()))
            ],
            "maintenance": {
                q: [round((a - zero) * 1000), round((b - zero) * 1000)]
                for q, (a, b) in self.maintenance.items()
            },
        }


_SETTINGS = {
    "auto_explain.log_min_duration": "0",
    "auto_explain.log_nested_statements": "on",
    "auto_explain.log_analyze": "on",
    "auto_explain.log_buffers": "on",
    "auto_explain.log_timing": "off",
    "auto_explain.log_format": "json",
    "auto_explain.log_level": "notice",
}


def _nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    out = [plan]
    for child in plan.get("Plans", []):
        out.extend(_nodes(child))
    return out


class AutoExplain:
    """Collects the plans the engine's backends execute while enabled, aggregated per (query, plan
    shape): count, generic or custom (a generic plan keeps ``$n`` in its conditions), buffers, rows,
    duration, first and last time seen; plus one full example plan per shape."""

    def __init__(
        self, superuser: Callable[[], Any], engine: AsyncEngine, sample_rate: float
    ) -> None:
        self.superuser, self.engine, self.sample_rate = superuser, engine, sample_rate
        self.shapes: dict[tuple[str, str], dict[str, Any]] = {}
        self.notices = 0
        self.unparsed = 0
        self._listening: set[asyncpg.Connection] = set()

    def _on_notice(self, _conn: Any, message: Any) -> None:
        text = getattr(message, "message", "") or ""
        if "plan:" not in text:
            return
        self.notices += 1
        now = time.monotonic()
        try:
            head, body = text.split("plan:", 1)
            duration = float(head.split("duration:")[1].split("ms")[0])
            doc = json.loads(body)
        except (IndexError, ValueError):
            self.unparsed += 1
            return
        plan = doc["Plan"]
        nodes = _nodes(plan)
        shape = " > ".join(
            n["Node Type"]
            + (f"[{n['Index Name']}]" if "Index Name" in n else "")
            + (f"({n['Relation Name']})" if "Relation Name" in n and "Index Name" not in n else "")
            for n in nodes
        )
        conds = json.dumps(
            [{k: v for k, v in n.items() if "Cond" in k or k == "Filter"} for n in nodes]
        )
        query = _query_head(doc.get("Query Text", ""))
        row = self.shapes.get((query, shape))
        if row is None:
            row = self.shapes[(query, shape)] = {
                "query": query, "shape": shape, "generic": "$" in conds, "count": 0,
                "duration_ms": 0.0, "max_ms": 0.0, "shared_blocks": 0, "max_shared_blocks": 0,
                "rows": 0, "first": now, "last": now, "example": doc,
            }  # fmt: skip
        blocks = plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0)
        row["count"] += 1
        row["duration_ms"] += duration
        row["max_ms"] = max(row["max_ms"], duration)
        row["shared_blocks"] += blocks
        row["max_shared_blocks"] = max(row["max_shared_blocks"], blocks)
        row["rows"] += plan.get("Actual Rows", 0)
        row["last"] = now

    def _on_checkout(self, dbapi_conn: Any, _record: Any, _proxy: Any) -> None:
        raw = _record.driver_connection
        if raw not in self._listening:
            raw.add_log_listener(self._on_notice)
            self._listening.add(raw)

    async def _configure(self, enable: bool) -> None:
        conn = await self.superuser()
        try:
            loaded = await conn.fetchval("SHOW shared_preload_libraries")
            assert "auto_explain" in loaded, f"auto_explain not preloaded: {loaded!r}"
            settings = {**_SETTINGS, "auto_explain.sample_rate": str(self.sample_rate)}
            for name, value in settings.items():
                if enable:
                    await conn.execute(f"ALTER SYSTEM SET {name} = '{value}'")
                else:
                    await conn.execute(f"ALTER SYSTEM RESET {name}")
            await conn.execute("SELECT pg_reload_conf()")
        finally:
            await conn.close()

    @contextlib.asynccontextmanager
    async def enabled(self) -> AsyncIterator[AutoExplain]:
        event.listen(self.engine.sync_engine, "checkout", self._on_checkout)
        await self._configure(True)
        try:
            yield self
        finally:
            await self._configure(False)
            event.remove(self.engine.sync_engine, "checkout", self._on_checkout)
            for raw in self._listening:
                with contextlib.suppress(Exception):
                    raw.remove_log_listener(self._on_notice)
            self._listening.clear()

    def record(self, zero: float) -> dict[str, Any]:
        rows = []
        for row in sorted(self.shapes.values(), key=lambda r: -r["duration_ms"]):
            n = row["count"]
            rows.append({
                "query": row["query"], "shape": row["shape"], "generic": row["generic"],
                "count": n, "mean_ms": round(row["duration_ms"] / n, 3), "max_ms": round(row["max_ms"], 3),
                "mean_shared_blocks": round(row["shared_blocks"] / n, 1),
                "max_shared_blocks": row["max_shared_blocks"], "mean_rows": round(row["rows"] / n, 1),
                "first_ms": round((row["first"] - zero) * 1000), "last_ms": round((row["last"] - zero) * 1000),
            })  # fmt: skip
        return {
            "sample_rate": self.sample_rate,
            "notices": self.notices,
            "unparsed": self.unparsed,
            "shapes": rows,
            "examples": {
                f"{r['query']} :: {r['shape']}": r["example"] for r in self.shapes.values()
            },
        }


def series_summary(series: list[tuple[int, int]], chunks: int = 10) -> dict[str, Any]:
    """Flat or rising: the statements in order, cut into ``chunks`` runs, each run's median and max
    statement ms (and the offset its run started at)."""
    if not series:
        return {"statements": 0}
    size = max(1, -(-len(series) // chunks))
    runs = []
    for k in range(0, len(series), size):
        part = series[k : k + size]
        ms = sorted(m for _, m in part)
        runs.append(
            {"from_ms": part[0][0], "n": len(part), "median": ms[len(ms) // 2], "max": ms[-1]}
        )
    return {"statements": len(series), "total_ms": sum(m for _, m in series), "runs": runs}
