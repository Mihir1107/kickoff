"""Output bytes never depend on the database's collation (ADR 0018 §28, §30).

A database is created with a tailored ICU collation that orders our identifiers otherwise than code
points (``C < G < D``: the dummy conversations ``C…``, ``G…``, ``D…``; digits reversed: unit days,
hex keys and ids), migrated from scratch, and corpus cases run on it AND on the default database
through collection, render, report and the job package. Expected:

- the RSMF (masked projection) equals the recorded golden generation, on both databases;
- ``units.jsonl`` is byte-identical between the two databases, and the report's sections derived
  from it (counts, unit exceptions, conversations) too;
- on the tailored database, the report recomputes from the job package (code-point order in Python,
  byte for byte), observations are in code-point order, the package's items and objects too.

The static half, every text column an ORDER BY in the product names: `test_ordered_text_columns`
in `tests/integration/db/test_collation.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_custody.export import export_package
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import upgrade
from edisc_db.session import create_engine, session_factory
from edisc_renderers.report.recompute import recompute
from edisc_worker.renders import RenderRun

from ..conftest import Connect
from ..normalizer.harness import new_tenant
from ..renders.conftest import drive, new_render, render_state
from ..report.test_report_model import build as build_report
from .cases import LIVE
from .check import check_against_oracle, check_golden, stored
from .oracle import build
from .test_corpus import collect

Sessions = async_sessionmaker[AsyncSession]
ICU_DB = "edisc_collation_icu"
# C < G < D (conversation prefixes), digits in reverse: unlike code points on every key we order by
ICU_RULES = "&C < G < D &[before 1]0 < 9 < 8 < 7 < 6 < 5 < 4 < 3 < 2 < 1 < 0"
PROBE = ["C00000DUMMY/2026-01-06", "C00000DUMMY/2026-01-05", "D00002DUMMY", "G00001DUMMY"]
RUN = ("custodians", "slack_three_epochs")


@dataclass
class Run:
    units: bytes
    report: dict[str, Any]
    observations: list[dict[str, Any]]


@pytest.fixture(scope="module")
async def icu(connect: Connect, settings: Settings) -> AsyncIterator[tuple[Settings, Sessions]]:
    su = await connect("superuser", db="postgres")
    try:
        await su.execute(f"DROP DATABASE IF EXISTS {ICU_DB} WITH (FORCE)")
        await su.execute(
            f"CREATE DATABASE {ICU_DB} TEMPLATE template0 ENCODING 'UTF8' LOCALE 'C'"
            f" LOCALE_PROVIDER icu ICU_LOCALE 'en-US' ICU_RULES '{ICU_RULES}'"
        )
    finally:
        await su.close()
    await bootstrap(settings, db=ICU_DB)
    await asyncio.to_thread(upgrade, ICU_DB)
    st = settings.model_copy(update={"pg_db": ICU_DB})
    engine = create_engine(st, "app", pool_size=10)
    yield st, session_factory(engine)
    await engine.dispose()
    su = await connect("superuser", db="postgres")
    try:
        await su.execute(f"DROP DATABASE IF EXISTS {ICU_DB} WITH (FORCE)")
    finally:
        await su.close()


async def test_the_tailored_database_orders_otherwise_than_code_points(
    connect: Connect, icu: tuple[Settings, Sessions]
) -> None:
    """The precondition, so the corpus test can never pass vacuously."""
    conn = await connect("owner", db=ICU_DB)
    try:
        got = await conn.fetchval("SELECT array_agg(x ORDER BY x) FROM unnest($1::text[]) x", PROBE)
        coded = await conn.fetchval(
            'SELECT array_agg(x ORDER BY x COLLATE "C") FROM unnest($1::text[]) x', PROBE
        )
    finally:
        await conn.close()
    assert coded == sorted(PROBE)
    assert got == [PROBE[0], PROBE[1], PROBE[3], PROBE[2]], got


async def _run(sessions: Sessions, s3: S3Client, settings: Settings, name: str, dest: Path) -> Run:
    """One case through collection, render (checked against the oracle and the golden), the report
    (recomputed from the job package) and the job package's own order."""
    case = LIVE[name]
    t = await new_tenant(sessions)
    job_id = await collect(sessions, s3, settings, t, case)
    render_id = await new_render(sessions, t.tenant_id, job_id, case.options)
    result = await drive(RenderRun(sessions, s3, settings), t.tenant_id, render_id)
    assert result["status"] == "completed", result
    st = await render_state(sessions, t.tenant_id, render_id)
    files = await stored(sessions, s3, settings, t.tenant_id, render_id)
    check_golden(name, check_against_oracle(case, build(case), files, dict(st["row"].summary)))
    built, report_files = await build_report(sessions, s3, settings, t, job_id)
    assert built.document["divergences"]["divergences"] == []
    pkg = await export_package(
        sessions, s3, settings, tenant_id=t.tenant_id, job_id=job_id, dest=dest,
        include_objects=False,
    )  # fmt: skip
    events = [json.loads(x) for x in (pkg / "events.jsonl").read_bytes().splitlines()]
    done = recompute(events, built.report_json, report_files.data["units.jsonl"])
    assert done.ok and done.database_rows == 0, done.differences
    # the package's own order: items by idempotency key within each batch, objects by storage key
    items = [json.loads(x) for x in (pkg / "items.jsonl").read_bytes().splitlines()]
    by_batch: dict[str, list[str]] = {}
    for it in items:
        by_batch.setdefault(it["custody_event_id"], []).append(it["idempotency_key"])
    assert all(keys == sorted(keys) for keys in by_batch.values())
    objects = [json.loads(x) for x in (pkg / "evidence.jsonl").read_bytes().splitlines()]
    keys = [(o["storage_key"], o["id"]) for o in objects]
    assert keys == sorted(keys)
    return Run(
        report_files.data["units.jsonl"], built.document, report_files.rows("observations.jsonl")
    )


@pytest.mark.parametrize("name", RUN)
async def test_the_corpus_is_byte_identical_under_another_collation(
    app_sessions: Sessions, s3: S3Client, settings: Settings, icu: tuple[Settings, Sessions],
    tmp_path: Path, name: str,
) -> None:  # fmt: skip
    icu_settings, icu_sessions = icu
    base = await _run(app_sessions, s3, settings, name, tmp_path / "default")
    tailored = await _run(icu_sessions, s3, icu_settings, name, tmp_path / "icu")
    assert tailored.units == base.units
    for section in ("counts", "conversations"):
        assert canonical_json(tailored.report[section]) == canonical_json(base.report[section])
    assert canonical_json(tailored.report["exceptions"]["units"]) == canonical_json(
        base.report["exceptions"]["units"]
    )
    # code-point order where the rows carry per-run ids
    obs = [(o["unit_key"], o["item_id"]) for o in tailored.observations]
    assert obs == sorted(obs) and len(obs) == len(base.observations)
