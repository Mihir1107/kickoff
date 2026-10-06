"""The collection report's model on real sealed jobs (ADR 0018 §15, M16 step 1): `units.jsonl`,
`observations.jsonl` and `report.json` against the oracle, the chain/database cross-check, the
recorded access facts, determinism."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.dataset import Dataset
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_renderers.report import model as m
from edisc_worker.report_loader import BuiltReport, ReportLoader, ReportRefusedError

from ..normalizer.harness import Tenant, new_tenant
from ..pipeline.conftest import run_job, spec
from . import oracle

Sessions = async_sessionmaker[AsyncSession]


class Files:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def __call__(self, name: str, chunks: AsyncIterator[bytes]) -> None:
        self.data[name] = b"".join([c async for c in chunks])

    def rows(self, name: str) -> list[dict[str, Any]]:
        body = self.data[name]
        assert body == b"" or body.endswith(b"\n")
        return [json.loads(line) for line in body.splitlines()]


async def build(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, job_id: uuid.UUID
) -> tuple[BuiltReport, Files]:
    files = Files()
    loader = ReportLoader(sessions, s3, settings, tenant_id=t.tenant_id, job_id=job_id)
    return await loader.build(files), files


def _assert_digests(built: BuiltReport, files: Files) -> None:
    import hashlib

    for f in built.files:
        rec = f.record()
        body = files.data[rec["name"]]
        assert (rec["sha256"], rec["size"], rec["rows"]) == (
            hashlib.sha256(body).hexdigest(), len(body), body.count(b"\n"),
        )  # fmt: skip


CASES: dict[str, dict[str, Any]] = {
    "clean": {},
    "gaps": {"failures": {"seed": 4, "drop_rate": 0.25}},
    "unverifiable": {"count_mode": "unavailable"},
    "access_lost": {"failures": {"inaccessible_from_epoch": {0: 0}}},
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_the_report_states_the_oracle(
    app_sessions: Sessions, s3: S3Client, settings: Settings, case: str
) -> None:
    sp = spec(dialect="slack_history", **CASES[case])
    t = await new_tenant(app_sessions)
    run = await run_job(app_sessions, s3, settings, t, sp, 0)
    built, files = await build(app_sessions, s3, settings, t, run.job_id)
    want = oracle.units(Dataset(sp), 0)
    status = oracle.job_status(want)
    assert run.status.value == status
    oracle.check(built.document, files.rows("units.jsonl"), want, status)
    assert built.document["custody_verification"]["ok"] is True
    assert built.document["divergences"]["divergences"] == []
    _assert_digests(built, files)
    # recorded access facts: this job was started after ADR 0018 §7.2
    access = built.document["access"]
    assert built.document["job"]["unit_day_zone"] == "UTC"
    assert access["source"] == "job_chain" and access["connector"] == "dummy"


async def test_no_longer_observed_across_a_rerun_is_listed_and_counted(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(dialect="slack_history")
    t = await new_tenant(app_sessions)
    await run_job(app_sessions, s3, settings, t, sp, 0)
    run = await run_job(app_sessions, s3, settings, t, sp, 1)
    built, files = await build(app_sessions, s3, settings, t, run.job_id)
    obs = files.rows("observations.jsonl")
    nlo = [o for o in obs if o["kind"] == "no_longer_observed"]
    units = files.rows("units.jsonl")
    assert nlo and sum(u["no_longer_observed"] or 0 for u in units) == len(nlo)
    assert obs == sorted(obs, key=lambda o: (o["unit_key"], o["item_id"]))
    counts = {r["value"]: r["count"] for r in built.document["exceptions"]["observations"]}
    assert counts["no_longer_observed"] == len(nlo)
    assert built.document["divergences"]["divergences"] == []


async def test_unavailable_files_are_observations_with_their_reasons(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(failures={"seed": 5, "file_unavailable_rate": 0.6})
    t = await new_tenant(app_sessions)
    run = await run_job(app_sessions, s3, settings, t, sp, 0)
    built, files = await build(app_sessions, s3, settings, t, run.job_id)
    unavailable = [o for o in files.rows("observations.jsonl") if o["kind"] == "file_unavailable"]
    assert unavailable and all(o["file_id"] and o["reason"] for o in unavailable)
    by_reason = {
        r["reason"]: r["count"] for r in built.document["exceptions"]["unavailable_files_by_reason"]
    }
    assert sum(by_reason.values()) == len(unavailable)


async def test_two_generations_are_byte_identical(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec(failures={"seed": 4, "drop_rate": 0.25})
    t = await new_tenant(app_sessions)
    run = await run_job(app_sessions, s3, settings, t, sp, 0)
    loader = ReportLoader(app_sessions, s3, settings, tenant_id=t.tenant_id, job_id=run.job_id)
    snapshot = await loader.snapshot()
    a, b = Files(), Files()
    first = await loader.build(a, snapshot=snapshot)
    second = await ReportLoader(
        app_sessions, s3, settings, tenant_id=t.tenant_id, job_id=run.job_id
    ).build(b, snapshot=snapshot)
    assert first.report_json == second.report_json and a.data == b.data


async def test_a_tampered_work_unit_is_a_divergence_and_the_chain_is_stated(
    app_sessions: Sessions, s3: S3Client, settings: Settings, connect: Any
) -> None:
    sp = spec()
    t = await new_tenant(app_sessions)
    run = await run_job(app_sessions, s3, settings, t, sp, 0)
    conn = await connect("superuser")
    try:
        key = await conn.fetchval(
            "SELECT unit_key FROM edisc.work_units WHERE job_id = $1 AND kind = 'conversation_day'"
            " ORDER BY unit_key LIMIT 1",
            run.job_id,
        )
        await conn.execute(
            "UPDATE edisc.work_units SET collected_count = collected_count - 1"
            " WHERE job_id = $1 AND unit_key = $2",
            run.job_id, key,
        )  # fmt: skip
    finally:
        await conn.close()
    built, files = await build(app_sessions, s3, settings, t, run.job_id)
    (d,) = built.document["divergences"]["divergences"]
    assert (d["kind"], d["subject"]) == ("unit_differs", key)
    assert d["database"]["collected"] == d["chain"]["collected"] - 1
    row = next(r for r in files.rows("units.jsonl") if r["unit_key"] == key)
    assert row["divergent"] and row["collected"] == d["chain"]["collected"]  # the chain is the fact
    assert built.document["job"]["clean"] is False and not built.clean
    assert built.document["banner"][0].startswith("RECORDS DISAGREE")


async def test_a_job_that_is_not_sealed_is_refused(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    t = await new_tenant(app_sessions)
    with pytest.raises(ReportRefusedError):
        await ReportLoader(
            app_sessions, s3, settings, tenant_id=t.tenant_id, job_id=uuid.uuid4()
        ).job()


async def _job_row(sessions: Sessions, t: Tenant, job_id: uuid.UUID) -> Any:
    async with tenant_tx(sessions, t.tenant_id) as s:
        return (
            await s.execute(text("SELECT * FROM collection_jobs WHERE id = :j"), {"j": job_id})
        ).one()


_ = m


async def test_overlapping_scopes_list_every_scope_covering_a_unit(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    from edisc_core.schemas import ScopeType

    from ..pipeline.test_multiscope import run_scoped, scope

    sp = spec()
    ds = Dataset(sp)
    first = ds.conversations()[0].id
    t = await new_tenant(app_sessions)
    job = await run_scoped(app_sessions, s3, settings, t, sp, [
        scope(ds, ScopeType.CHANNEL, "*", 0, 2),  # every conversation, both days
        scope(ds, ScopeType.CHANNEL, first, 1, 1),  # one conversation, second day
    ])  # fmt: skip
    built, files = await build(app_sessions, s3, settings, t, job)
    scopes = built.document["scopes"]["scopes"]
    assert [s["id"] for s in scopes] == ["*", first]
    for row in files.rows("units.jsonl"):
        if row["kind"] == "directory":
            continue
        both = row["conversation_id"] == first and row["day"] == ds.day(1).isoformat()
        assert row["scopes"] == ([0, 1] if both else [0]), row


async def test_a_retention_gap_touching_the_job_makes_it_not_clean(
    app_sessions: Sessions, s3: S3Client, settings: Settings, connect: Any
) -> None:
    sp = spec()
    t = await new_tenant(app_sessions)
    run = await run_job(app_sessions, s3, settings, t, sp, 0)
    conn = await connect("superuser")
    try:  # the injected condition: one evidence object of the job was unprotected for a while
        await conn.execute(
            "INSERT INTO edisc.retention_gaps (id, tenant_id, evidence_object_id, owner_type,"
            " owner_id, unprotected_from, unprotected_until, outcome)"
            " SELECT gen_random_uuid(), e.tenant_id, e.id, 'matter', $2, now() - interval '2 days',"
            " now() - interval '1 day', 'relocked' FROM edisc.evidence_objects e"
            " WHERE e.job_id = $1 ORDER BY e.id LIMIT 1",
            run.job_id, t.matter_id,
        )  # fmt: skip
    finally:
        await conn.close()
    built, _ = await build(app_sessions, s3, settings, t, run.job_id)
    assert built.document["job"]["status"] == "completed"
    assert len(built.document["evidence_store"]["retention_gaps"]) == 1
    assert built.document["job"]["clean"] is False and not built.clean


async def test_a_job_started_before_the_access_facts_prints_unknown(
    app_sessions: Sessions, s3: S3Client, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-ADR 0018 §7.2 `job_started` (no plan tier, scopes, blind spots or zone)."""
    import edisc_worker.pipeline as pipeline

    original = pipeline.append

    async def old_job_started(*args: Any, **kw: Any) -> Any:
        if kw.get("event_type") == "job_started":
            kw["payload"] = {
                k: v for k, v in kw["payload"].items()
                if k not in ("plan_tier", "granted_scopes", "blind_spots", "unit_day_zone", "export")
            }  # fmt: skip
        return await original(*args, **kw)

    monkeypatch.setattr(pipeline, "append", old_job_started)
    sp = spec()
    t = await new_tenant(app_sessions)
    run = await run_job(app_sessions, s3, settings, t, sp, 0)
    monkeypatch.undo()
    built, files = await build(app_sessions, s3, settings, t, run.job_id)
    access = built.document["access"]
    assert access["source"] == "not_recorded"
    for key in ("plan_tier", "granted_scopes", "blind_spots"):
        assert access[key] == m.UNKNOWN
    assert built.document["job"]["unit_day_zone"] == m.ZONE_NOT_RECORDED
    assert built.document["job"]["unit_day_zone_source"] == "not_recorded"
    assert {r["zone"] for r in files.rows("units.jsonl")} == {m.ZONE_NOT_RECORDED}
