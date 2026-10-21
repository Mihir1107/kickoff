"""The synthetic render corpus, export dialect (M15 step 5): upload, lock and validate the synthetic
export through the API, collect from it, render, then the same checks as the live cases.

Hostile export values (ADR 0018 §28): a channel NAME holding a lone surrogate is text (kept exactly,
nothing refused, every message collected and rendered); a channel ID holding one cannot key items
(the conversation is listed under a stand-in id and each of its day files is a GAP unit with the
reason; its messages stay in the locked export, none is collected); a message ``ts`` holding one
cannot be an item (that element is a gap of its unit, the others are collected) and a ``thread_ts``
holding one cannot be looked up (the unit is a gap although every message is collected). Each case
then gets a report whose ``units.jsonl`` is rebuilt from the job package byte for byte."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import (
    HIGH,
    LOW,
    ExportManifest,
    ExportOptions,
    write_export,
)
from edisc_connector_slack_export.layout import BLIND_SPOTS_ALL, BLIND_SPOTS_PUBLIC_ONLY
from edisc_custody.export import export_package
from edisc_db.session import tenant_tx
from edisc_renderers.report.html import escape
from edisc_verify.canonical import canonical_json
from edisc_verify.lone_surrogates import replacement, substitute_key, utf16_units
from edisc_verify.report.recompute import recompute
from edisc_worker.renders import RenderRun

from ..corpus.cases import EXPORT, Case
from ..corpus.check import (
    check_against_oracle,
    check_golden,
    check_job_package,
    check_package,
    stored,
)
from ..corpus.oracle import build
from ..renders.conftest import drive, new_render, render_state
from ..report.test_report_model import build as build_report
from .conftest import Api, FileHost, TenantCtx, collection_workers
from .test_export_collection import _export_connection, _job, _scope
from .test_jobs import make_world


@pytest.mark.parametrize("name", sorted(EXPORT))
async def test_export_case(api: Api, tenant: TenantCtx, tmp_path: Path, name: str) -> None:
    case = EXPORT[name]
    ds = Dataset(case.spec)
    buf = io.BytesIO()
    manifest = write_export(
        ds, buf, ExportOptions(tier=case.export_tier, legacy_layout=case.export_legacy_layout,
                               hostile=case.export_hostile)
    )  # fmt: skip
    plan = {} if case.export_plan is None else {"plan": case.export_plan}
    oracle = build(case)
    async with collection_workers(api, FileHost(ds)) as exp:
        w = await make_world(exp, tenant, case.spec)
        start = datetime.combine(ds.day(case.first_day), datetime.min.time(), tzinfo=UTC)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, buf.getvalue(), **plan)
            job = await _job(
                c, w.matter,
                {"connection_id": export["connection_id"],
                 "scopes": [_scope(start, ds.n_days(0) - case.first_day, case.policy.value)]},
            )  # fmt: skip
        await _check_export_report(exp, tenant, case, buf.getvalue(), manifest, job, tmp_path)
        rs = exp.settings.model_copy(update={"render_files_batch_size": case.batch_size})
        job_id = uuid.UUID(job["id"])
        render_id = await new_render(exp.sessions, tenant.tenant_id, job_id, case.options)
        result = await drive(RenderRun(exp.sessions, exp.s3, rs), tenant.tenant_id, render_id)
        assert result["status"] == "completed", result
        st = await render_state(exp.sessions, tenant.tenant_id, render_id)
        files = await stored(exp.sessions, exp.s3, rs, tenant.tenant_id, render_id)
        manifests = await asyncio.to_thread(  # pure CPU: never on the loop (ADR 0015 §24.19)
            check_against_oracle, case, oracle, files, dict(st["row"].summary)
        )
        await asyncio.to_thread(check_golden, name, manifests)
        await check_package(exp.sessions, exp.s3, rs, tenant.tenant_id, render_id, tmp_path / "pkg")
        if case.spec.lone_surrogates:
            await check_job_package(
                exp.sessions, exp.s3, rs, tenant.tenant_id, job_id, tmp_path / "job"
            )
        if case.export_hostile is not None:
            await _check_hostile(exp, tenant, ds, manifest, job, export, tmp_path)
            if case.export_hostile == "channel_name":  # the RSMF names it validly and exactly
                (conv,) = {
                    json.dumps(m["conversations"][0], sort_keys=True)
                    for m in manifests.values()
                    if m["conversations"][0]["id"] == ds.conversations()[0].id
                }
                named = json.loads(conv)
                exact = ds.conversations()[0].name + HIGH
                assert named["display"] == replacement(exact)
                lone = [c["value"] for c in named["custom"] if c["name"] == "edisc.lone_surrogates"]
                units = " ".join(f"{u:04x}" for u in utf16_units(exact))
                assert f"/display: {units}" in lone  # the name, and its `edisc.known_name` copy
                assert {v.split(": ", 1)[1] for v in lone} == {units}


async def _check_export_report(
    api: Api, tenant: TenantCtx, case: Case, data: bytes, manifest: ExportManifest,
    job: dict[str, Any], tmp_path: Path,
) -> None:  # fmt: skip
    """The report states the export the job read, from the chain (ADR 0018 §32): the declared plan,
    the tier detected from the archive, what the export cannot contain (its blind spots), the tier
    warnings and the validation findings; the verifier recomputes it from the job package, and the
    HTML says it. Expected values come from the case and the product's own statements (`layout`),
    the findings from the export record (the chain must equal it)."""
    job_id = uuid.UUID(job["id"])
    built, files = await build_report(api.sessions, api.s3, api.settings, tenant, job_id)
    stated = built.document["access"]["export"]
    tier = case.export_tier
    spots = list(BLIND_SPOTS_ALL + (BLIND_SPOTS_PUBLIC_ONLY if tier == "public_only" else ()))
    assert {k: stated[k] for k in ("sha256", "size_bytes", "entry_count", "declared_plan",
                                   "detected_tier", "tier_confirmed", "blind_spots")} == {
        "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
        "entry_count": manifest.entries, "declared_plan": case.export_plan, "detected_tier": tier,
        "tier_confirmed": tier != "grid", "blind_spots": spots,
    }  # fmt: skip
    grid_warning = "Enterprise Grid structure suspected"
    assert any(grid_warning in w for w in stated["tier_warnings"]) == (tier == "grid")
    if tier != "grid":  # each case declares the plan that normally produces its tier
        assert stated["tier_warnings"] == []
    findings, *_ = await _state(api, tenant, job_id, job["connection_id"])
    assert stated["findings"] == {
        k: v for k, v in findings.items() if k not in ("blind_spots", "tier_warnings")
    }
    assert (stated["blind_spots"], stated["tier_warnings"]) == (
        findings["blind_spots"], findings["tier_warnings"],
    )  # fmt: skip
    assert stated["findings"]["metadata_files"] == sorted(manifest.metadata_files)
    pkg = await export_package(api.sessions, api.s3, api.settings, tenant_id=tenant.tenant_id,
                               job_id=job_id, dest=tmp_path / "export-report-job",
                               include_objects=False, archives="reference")  # fmt: skip
    events = [json.loads(x) for x in (pkg / "events.jsonl").read_bytes().splitlines()]
    done = recompute(events, built.report_json, files.data["units.jsonl"])
    assert done.ok, done.differences
    # a report stating another tier or plan than the chain recorded fails the recomputation
    forged = json.loads(built.report_json)
    forged["access"]["export"] |= {"detected_tier": "full", "declared_plan": "enterprise_grid",
                                   "blind_spots": list(BLIND_SPOTS_ALL[:1])}  # fmt: skip
    if forged["access"]["export"] != built.document["access"]["export"]:
        bad = recompute(events, canonical_json(forged), files.data["units.jsonl"])
        assert not bad.ok and any("access" in d for d in bad.differences), bad.differences
    page = files.data["report.html"].decode("utf-8")
    for spot in spots:
        assert escape(spot) in page
    assert ("not in this export" in page) == (tier == "public_only")


def _day_counts(ds: Dataset, conversation: str) -> dict[str, int]:
    """Elements of each day file the export writer wrote for the conversation (its expectation)."""
    out = {}
    for d in range(ds.n_days(0)):
        n = sum(1 for m in ds.unit_messages(conversation, d, 0) if m.deleted_ts is None)
        if n:
            out[ds.day(d).isoformat()] = n
    return out


async def _state(api: Api, tenant: TenantCtx, job_id: uuid.UUID, connection: str) -> Any:
    async with tenant_tx(api.sessions, tenant.tenant_id) as s:
        findings = (await s.execute(text(
            "SELECT findings FROM slack_exports WHERE connection_id = :c"
        ), {"c": uuid.UUID(connection)})).scalar_one()  # fmt: skip
        conversations = (await s.execute(text(
            "SELECT x.* FROM export_conversations x JOIN slack_exports e ON e.id = x.export_id"
            " WHERE e.connection_id = :c ORDER BY x.conversation_id"
        ), {"c": uuid.UUID(connection)})).all()  # fmt: skip
        units = (await s.execute(text(
            "SELECT * FROM work_units WHERE job_id = :j AND kind = 'conversation_day'"
        ), {"j": job_id})).all()  # fmt: skip
        items = (await s.execute(text(
            "SELECT i.source_item_id, i.item_type, ji.unit_key FROM job_items ji JOIN items i"
            " ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id WHERE ji.job_id = :j"
        ), {"j": job_id})).all()  # fmt: skip
    return findings, conversations, units, items


async def _report_recomputes(
    api: Api, tenant: TenantCtx, job_id: uuid.UUID, tmp_path: Path
) -> list[dict[str, Any]]:
    """The job's report, and its units.jsonl rebuilt from the job package byte for byte."""
    built, files = await build_report(api.sessions, api.s3, api.settings, tenant, job_id)
    assert built.document["divergences"]["divergences"] == []
    pkg = await export_package(api.sessions, api.s3, api.settings, tenant_id=tenant.tenant_id,
                               job_id=job_id, dest=tmp_path / "hostile-job",
                               include_objects=False, archives="reference")  # fmt: skip
    events = [json.loads(x) for x in (pkg / "events.jsonl").read_bytes().splitlines()]
    done = recompute(events, built.report_json, files.data["units.jsonl"])
    assert done.ok and done.database_rows == 0, done.differences
    return files.rows("units.jsonl")


async def _check_hostile(
    api: Api, tenant: TenantCtx, ds: Dataset, manifest: ExportManifest, job: dict[str, Any],
    export: dict[str, Any], tmp_path: Path,
) -> None:  # fmt: skip
    """Exactly what happens to conversation 0's messages (ADR 0018 §28)."""
    conv0 = ds.conversations()[0]
    counts = _day_counts(ds, conv0.id)
    job_id = uuid.UUID(job["id"])
    findings, conversations, units, items = await _state(
        api, tenant, job_id, export["connection_id"]
    )
    ws = ds.spec.workspace_id
    of_conv0 = [i for i in items if i.source_item_id.startswith(f"{ws}/{conv0.id}/")]
    rows = {r["unit_key"]: r for r in await _report_recomputes(api, tenant, job_id, tmp_path)}
    if "name" in manifest.hostile:  # text: kept exactly, nothing refused, everything collected
        (row,) = [c for c in conversations if c.conversation_id == conv0.id]
        assert (row.name, row.folder) == (conv0.name + "\ufffd",) * 2 and row.refused is None
        assert row.exact == {"/name": utf16_units(conv0.name + HIGH)}
        assert findings["metadata_records_with_lone_surrogates"] == 1
        assert findings["conversations_refused"] == {}
        assert job["status"] == "completed_against_archive"
        mine = [u for u in units if u.conversation_id == conv0.id]
        assert {u.day.isoformat(): u.collected_count for u in mine} == counts
        assert {u.recon_status for u in mine} == {"matched_against_archive"}
        assert len({i.source_item_id for i in of_conv0 if i.item_type == "message"}) >= sum(
            counts.values()
        )
        return
    # an id: refused as a key, listed under a stand-in, every day file a gap with the reason
    stand_in = substitute_key(conv0.id + LOW)
    (row,) = [c for c in conversations if c.conversation_id == stand_in]
    assert row.refused == "conversation_id_lone_surrogate" and row.name == conv0.name
    assert row.exact == {"/id": utf16_units(conv0.id + LOW)}
    assert findings["conversations_refused"] == {"conversation_id_lone_surrogate": 1}
    assert job["status"] == "completed_with_gaps"
    mine = [u for u in units if u.conversation_id == stand_in]
    assert {u.day.isoformat(): u.expected_count for u in mine} == counts  # every day file
    for u in mine:
        assert (u.status, u.recon_status, u.collected_count) == ("done", "gap", 0), u
        assert u.gap_reasons == ["conversation_id_lone_surrogate"]
        r = rows[u.unit_key]
        assert (r["source"], r["recon_status"], r["gap_reasons"]) == (
            "job_chain", "gap", ["conversation_id_lone_surrogate"],
        )  # fmt: skip
    assert not [u for u in units if u.conversation_id == conv0.id]
    assert of_conv0 == []  # none of its messages is an item; their bytes stay in the export
    others = [u for u in units if u.conversation_id != stand_in]
    assert others and {u.recon_status for u in others} == {"matched_against_archive"}


async def test_export_message_keys_with_lone_surrogates_are_gaps_of_their_units(
    api: Api, tenant: TenantCtx, tmp_path: Path
) -> None:
    """A ``ts`` holding a lone surrogate: that element cannot be an item, the unit's other messages
    are collected and the unit is a gap (``ts_lone_surrogate``), never a failure of the whole unit. A
    ``thread_ts`` holding one: every message is collected (its derivation keeps the exact value), the
    thread cannot be looked up, the unit is a gap (``thread_ts_lone_surrogate``)."""
    case = EXPORT["export_lone_surrogates"]
    ds = Dataset(case.spec.model_copy(update={"lone_surrogates": False}))
    buf = io.BytesIO()
    manifest = write_export(ds, buf, ExportOptions(hostile="message_keys"))
    assert {"ts", "thread_ts"} <= set(manifest.hostile)
    async with collection_workers(api, FileHost(ds)) as exp:
        w = await make_world(exp, tenant, ds.spec)
        start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, buf.getvalue())
            job = await _job(c, w.matter, {"connection_id": export["connection_id"],
                                           "scopes": [_scope(start, ds.n_days(0))]})  # fmt: skip
        job_id = uuid.UUID(job["id"])
        findings, _, units, items = await _state(exp, tenant, job_id, export["connection_id"])
        rows = {r["unit_key"]: r for r in await _report_recomputes(exp, tenant, job_id, tmp_path)}
    assert job["status"] == "completed_with_gaps"
    assert findings["thread_ts_lone_surrogate"] == 1 and findings["ts_lone_surrogate"] == 1
    conv, day, ts = manifest.hostile["ts"]
    counts = _day_counts(ds, conv)
    key = f"{conv}/{ds.day(day).isoformat()}"
    (u,) = [u for u in units if u.unit_key == key]
    assert (u.recon_status, u.expected_count, u.collected_count) == (
        "gap", counts[ds.day(day).isoformat()], counts[ds.day(day).isoformat()] - 1,
    )  # fmt: skip
    assert u.gap_reasons == ["ts_lone_surrogate"] and rows[key]["gap_reasons"] == u.gap_reasons
    ws = ds.spec.workspace_id
    assert f"{ws}/{conv}/{ts}" not in {i.source_item_id for i in items}  # the element, not an item
    conv, day, reply = manifest.hostile["thread_ts"]
    key = f"{conv}/{ds.day(day).isoformat()}"
    (u,) = [u for u in units if u.unit_key == key]
    assert (u.recon_status, u.collected_count) == ("gap", u.expected_count)
    assert (
        u.gap_reasons == ["thread_ts_lone_surrogate"] and rows[key]["gap_reasons"] == u.gap_reasons
    )
    assert f"{ws}/{conv}/{reply}" in {i.source_item_id for i in items}  # collected
    clean = [u for u in units if not u.gap_reasons]
    assert clean and {u.recon_status for u in clean} == {"matched_against_archive"}
