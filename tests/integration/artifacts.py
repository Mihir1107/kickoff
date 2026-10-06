"""Failure artifacts for CI (`EDISC_TEST_ARTIFACTS_DIR`): a failing integration test leaves enough to
diagnose it without reproducing it first (CLAUDE.md: never re-run to green without a diagnosis).

For every test that failed (in setup, call or teardown), at the end of the session, while the test
stack is still up:
- ``<test>/worker-logs/``: every ``*.log`` under the test's ``tmp_path`` (the logs of the worker
  processes it spawned);
- ``<test>/temporal/<workflow id>__<run id>.json``: the full history of every workflow started
  during the test (Temporal visibility, by start time; at most `MAX_WORKFLOWS`);
- ``<test>/custody_events.jsonl``: every custody stream that got an event during the test, whole
  (superuser read, ordered by stream and seq; at most `MAX_EVENTS` rows).
- ``<test>/test.json``: node id, phases that failed, the time window.
Nothing here runs unless a test failed and the variable is set.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
from temporalio.client import Client

from edisc_core.settings import Settings
from edisc_core.time import utc_now

MAX_WORKFLOWS = 200
MAX_EVENTS = 200_000
SLACK = timedelta(seconds=2)


@dataclass
class Failed:
    nodeid: str
    start: datetime
    stop: datetime
    phases: list[str] = field(default_factory=list)
    tmp_path: Path | None = None

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", self.nodeid)[-150:]


def _copy_logs(src: Path, dest: Path) -> int:
    n = 0
    for path in sorted(src.rglob("*.log")):
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        n += 1
    return n


async def _histories(client: Client, f: Failed, dest: Path) -> int:
    start, stop = (f.start - SLACK).isoformat(), (f.stop + SLACK).isoformat()
    query = f'StartTime BETWEEN "{start}" AND "{stop}"'
    n = 0
    async for wf in client.list_workflows(query, limit=MAX_WORKFLOWS):
        history = await client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history()
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{wf.id}__{wf.run_id}")
        (dest / f"{name}.json").write_text(history.to_json())
        n += 1
    return n


async def _custody(conn: asyncpg.Connection, f: Failed, dest: Path) -> int:
    rows = await conn.fetch(
        "SELECT * FROM edisc.custody_events WHERE stream_id IN ("
        "  SELECT DISTINCT stream_id FROM edisc.custody_events"
        "  WHERE created_at BETWEEN $1 AND $2)"
        " ORDER BY stream_id, seq LIMIT $3",
        f.start - SLACK, f.stop + SLACK, MAX_EVENTS,
    )  # fmt: skip
    with dest.open("w") as fh:
        for r in rows:
            fh.write(json.dumps({k: _plain(v) for k, v in dict(r).items()}, sort_keys=True) + "\n")
    return len(rows)


def _plain(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, str | int | float | bool) or v is None:
        return v
    return str(v)


async def _collect(settings: Settings, failed: list[Failed], root: Path) -> list[str]:
    notes: list[str] = []
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    conn = await asyncpg.connect(settings.pg_dsn("superuser"))
    try:
        for f in failed:
            out = root / f.slug
            (out / "temporal").mkdir(parents=True, exist_ok=True)
            logs = _copy_logs(f.tmp_path, out / "worker-logs") if f.tmp_path else 0
            wfs = await _histories(client, f, out / "temporal")
            events = await _custody(conn, f, out / "custody_events.jsonl")
            (out / "test.json").write_text(json.dumps({
                "nodeid": f.nodeid, "phases": f.phases, "start": f.start.isoformat(),
                "stop": f.stop.isoformat(), "worker_logs": logs, "workflows": wfs,
                "custody_events": events,
            }, indent=2))  # fmt: skip
            notes.append(f"{f.nodeid}: {logs} logs, {wfs} workflow histories, {events} events")
    finally:
        await conn.close()
    return notes


def collect(failed: list[Failed], root: Path) -> list[str]:
    """Called from ``pytest_sessionfinish`` (no loop is running there)."""
    root.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_collect(Settings(), failed, root))


def now() -> datetime:
    return utc_now()
