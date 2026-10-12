"""Every unit fact the report states is in the job chain (ADR 0018 §28): unit events carry the unit's
kind, conversation, scopes, access-lost reason and gap reasons; units left unsettled by a cancelled
job are recorded (`units_unsettled`); the report then states every unit row from the chain, and
`units.jsonl` is rebuilt from the job's custody package byte for byte. A job recorded before (no
`unit_event_format`) states UNKNOWN for what its chain does not say. The report's file order does not
depend on the database's locale (the order columns are COLLATE "C")."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from types_aiobotocore_s3 import S3Client

import edisc_worker.pipeline as pipeline_module
from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connectors_base.types import Connection
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.export import export_package
from edisc_db.session import tenant_tx
from edisc_renderers.report import model as m
from edisc_renderers.report.recompute import recompute
from edisc_worker.pipeline import CrashHooks, Pipeline

from ...unit.dummy.conftest import RecordingLimiter
from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..pipeline.conftest import run_job, spec
from .test_report_model import build

DETAIL = ("kind", "conversation_id", "scopes", "access_lost_reason", "gap_reasons")


async def _events(sessions: Sessions, t: Tenant, job_id: uuid.UUID) -> list[Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT event_type, payload FROM custody_events WHERE stream_id = :j"
                        " ORDER BY seq"
                    ),
                    {"j": job_id},
                )
            ).all()
        )


async def _units(sessions: Sessions, t: Tenant, job_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_tx(sessions, t.tenant_id) as s:
        rows = (
            await s.execute(text("SELECT * FROM work_units WHERE job_id = :j"), {"j": job_id})
        ).all()
    return {r.unit_key: r for r in rows}


async def _recomputed(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, job_id: uuid.UUID,
    tmp: Path, files: Any, built: Any,
) -> Any:  # fmt: skip
    pkg = await export_package(sessions, s3, settings, tenant_id=t.tenant_id, job_id=job_id,
                               dest=tmp / "job", include_objects=False)  # fmt: skip
    events = [json.loads(line) for line in (pkg / "events.jsonl").read_bytes().splitlines()]
    return recompute(events, built.report_json, files.data["units.jsonl"])


async def test_every_unit_event_records_the_units_database_facts(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path
) -> None:
    sp = spec(dialect="slack_history", failures={"inaccessible_from_epoch": {0: 0}})
    t = await new_tenant(app_sessions)
    run = await run_job(app_sessions, s3, settings, t, sp, 0)
    events = await _events(app_sessions, t, run.job_id)
    units = await _units(app_sessions, t, run.job_id)
    assert events[0].event_type == "job_started"
    assert events[0].payload["unit_event_format"] == 2
    seen = {}
    for e in events:
        if e.event_type in ("unit_reconciled", "unit_failed"):
            u = units[e.payload["unit_key"]]
            want = {"kind": u.kind, "conversation_id": u.conversation_id, "scopes": [] if u.kind == "directory" else [0],
                    "access_lost_reason": u.access_lost_reason, "gap_reasons": u.gap_reasons}  # fmt: skip
            assert {k: e.payload[k] for k in DETAIL} == want, e.payload
            seen[u.unit_key] = e.payload
    assert set(seen) == set(units)  # every unit, the directory unit included
    assert any(p["access_lost_reason"] for p in seen.values())  # the access-lost reason recorded
    assert seen[m.DIRECTORY_UNIT_KEY]["kind"] == "directory"

    built, files = await build(app_sessions, s3, settings, t, run.job_id)
    rows = files.rows("units.jsonl")
    assert {r["source"] for r in rows} == {"job_chain"}
    assert built.document["divergences"]["divergences"] == []
    lost = [r for r in rows if r["recon_status"] == "access_lost"]
    assert lost and all(r["access_lost_reason"] == units[r["unit_key"]].access_lost_reason
                        for r in lost)  # fmt: skip
    done = await _recomputed(app_sessions, s3, settings, t, run.job_id, tmp_path, files, built)
    assert done.ok, done.differences
    assert done.database_rows == 0 and done.units_rows == len(rows)


async def _cancelled_job(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, settle: int
) -> uuid.UUID:
    """A job cancelled after ``settle`` units were collected and reconciled: the rest unsettled."""
    sp = spec()
    ds = Dataset(sp)
    conn = Connection(t.tenant_id, t.connection_id, "dummy", sp.workspace_id,
                      {"spec": sp.model_dump(mode="json"), "epoch": 0})  # fmt: skip
    scope = scope_for_days(
        "*", datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC), ds.n_days(0)
    )
    job_id = new_id()
    p = Pipeline(sessions, s3, settings, DummyConnector(RecordingLimiter()), CrashHooks())
    await p.start_job(tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
                      connection_id=t.connection_id, scopes=[scope], requested_by="tester")  # fmt: skip
    await p.enumerate_units(tenant_id=t.tenant_id, job_id=job_id, conn=conn)
    for key in (await p.pending_units(t.tenant_id, job_id))[:settle]:
        while (await p.collect_pages(tenant_id=t.tenant_id, job_id=job_id, unit_key=key,
                                     conn=conn)).value == "more":  # fmt: skip
            pass
        await p.finalize_unit(tenant_id=t.tenant_id, job_id=job_id, unit_key=key, conn=conn)
    await p.request_stop(tenant_id=t.tenant_id, job_id=job_id, reason="cancel", detail="test",
                         actor="user:carol")  # fmt: skip
    await p.finalize_job(tenant_id=t.tenant_id, job_id=job_id)
    return job_id


async def test_a_cancelled_job_records_its_unsettled_units_and_the_report_states_them(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(pipeline_module, "UNSETTLED_BATCH", 2)  # several `units_unsettled` events
    t = await new_tenant(app_sessions)
    job_id = await _cancelled_job(app_sessions, s3, settings, t, settle=2)
    units = await _units(app_sessions, t, job_id)
    unsettled = {k for k, u in units.items() if u.status not in ("done", "failed")}
    assert len(unsettled) >= 3
    recorded: list[dict[str, Any]] = []
    events = await _events(app_sessions, t, job_id)
    for e in events:
        if e.event_type == "units_unsettled":
            assert len(e.payload["units"]) <= 2
            recorded.extend(e.payload["units"])
    assert [e.event_type for e in events][-1] == "job_cancelled"
    assert {u["unit_key"] for u in recorded} == unsettled
    for u in recorded:
        row = units[u["unit_key"]]
        assert (u["status"], u["recon_status"], u["kind"]) == (
            row.status,
            row.recon_status,
            row.kind,
        )

    built, files = await build(app_sessions, s3, settings, t, job_id)
    assert built.document["job"]["status"] == "cancelled"
    assert built.document["divergences"]["divergences"] == []
    rows = {r["unit_key"]: r for r in files.rows("units.jsonl")}
    assert set(rows) == set(units) and {r["source"] for r in rows.values()} == {"job_chain"}
    assert all(rows[k]["status"] == units[k].status for k in unsettled)
    done = await _recomputed(app_sessions, s3, settings, t, job_id, tmp_path, files, built)
    assert done.ok, done.differences
    assert done.database_rows == 0


async def test_a_job_recorded_before_states_unknown_and_its_unsettled_units_from_the_database(
    app_sessions: Sessions, s3: S3Client, settings: Settings, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """No `unit_event_format` in `job_started`: whatever its later events carry, the chain is read
    as it was before §28. Settled rows state UNKNOWN for the scopes, access-lost reason and gap
    reasons (kind and conversation from the unit key); unsettled rows come from the database and are
    named by the recomputation as taken as written."""
    original = pipeline_module.append

    async def old_job_started(*args: Any, **kw: Any) -> Any:
        if kw.get("event_type") == "job_started":
            kw["payload"] = {k: v for k, v in kw["payload"].items() if k != "unit_event_format"}
        return await original(*args, **kw)

    monkeypatch.setattr(pipeline_module, "append", old_job_started)
    t = await new_tenant(app_sessions)
    job_id = await _cancelled_job(app_sessions, s3, settings, t, settle=2)
    monkeypatch.undo()
    built, files = await build(app_sessions, s3, settings, t, job_id)
    assert built.document["divergences"]["divergences"] == []  # like with like
    rows = files.rows("units.jsonl")
    chain_rows = [r for r in rows if r["source"] == "job_chain"]
    db_rows = [r for r in rows if r["source"] == "database"]
    assert len(chain_rows) == 2 and len(db_rows) >= 3
    for r in chain_rows:
        assert (r["scopes"], r["access_lost_reason"], r["gap_reasons"]) == (m.UNKNOWN,) * 3
        assert r["kind"] == m.kind_of(r["unit_key"]) and r["conversation_id"]
    done = await _recomputed(app_sessions, s3, settings, t, job_id, tmp_path, files, built)
    assert done.ok, done.differences
    assert done.database_rows == len(db_rows)
    assert any("taken as written" in line for line in done.not_recomputed())


async def test_the_report_order_does_not_depend_on_the_database_locale(
    app_sessions: Sessions,
) -> None:
    """`units.jsonl` and `observations.jsonl` are written in code-point order (the offline verifier
    rebuilds them in Python); a glibc `en_US` collation would order "a", "B", "é" otherwise."""
    async with tenant_tx(app_sessions, uuid.uuid4()) as s:
        cols = (
            await s.execute(
                text(
                    "SELECT table_name, column_name, collation_name FROM information_schema.columns"
                    " WHERE table_schema = 'edisc' AND (table_name, column_name) IN (('work_units',"
                    " 'unit_key'), ('work_units', 'conversation_id'), ('job_items', 'unit_key'),"
                    " ('work_unit_scopes', 'unit_key'))"
                )
            )
        ).all()
        assert sorted((c.table_name, c.column_name, c.collation_name) for c in cols) == [
            ("job_items", "unit_key", "C"), ("work_unit_scopes", "unit_key", "C"),
            ("work_units", "conversation_id", "C"), ("work_units", "unit_key", "C"),
        ]  # fmt: skip
        values = ["a", "B", "é", "Z", "_x", "lone-surrogate:0043.dc00", "C01"]
        ordered = (
            await s.execute(
                text('SELECT v FROM unnest(CAST(:v AS text[])) v ORDER BY v COLLATE "C"'),
                {"v": values},
            )
        ).scalars()
        assert list(ordered) == sorted(values)
