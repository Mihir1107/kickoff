"""Real-world Slack export variants (M14.4), generated from the dummy oracle and ingested through the
real API and worker: macOS re-zips, a wrapper folder, ZIP64 (forced and by entry count), streaming zips
with data descriptors, and non-ASCII names with and without the UTF-8 flag. None is rejected; every
irregularity is reported, nothing is mangled silently."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportManifest, ExportOptions, write_export
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connector_dummy.zipwriter import ZipWriter
from tests.integration.pg_diagnostics import AutoExplain, WaitSampler, series_summary
from tests.integration.phase_timings import PhaseClock, phase_clock
from tests.logcapture import captured_logs

from ..custody.conftest import superuser
from .conftest import Api, TenantCtx
from .test_exports import settled, upload

SPEC = DatasetSpec(seed=11, conversations=4, days=2, messages_per_unit=12)


def generate(opts: ExportOptions) -> tuple[bytes, ExportManifest]:
    buf = io.BytesIO()
    manifest = write_export(Dataset(SPEC), buf, opts)
    return buf.getvalue(), manifest


async def ingest(api: Api, tenant: TenantCtx, data: bytes) -> dict[str, Any]:
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        export_id, done = await upload(c, tenant.default_client_id, data)
        assert done.status_code == 202, done.text
        return await settled(c, export_id)


def assert_complete_mapping(out: dict[str, Any], manifest: ExportManifest) -> None:
    """Every folder maps to a listed conversation and every conversation has its folder."""
    f = out["findings"]
    assert out["status"] == "ready", out
    assert out["entry_count"] == manifest.entries
    assert f["entries_by_kind"]["day"] == manifest.day_files
    assert f["conversations"] == len(manifest.conversations)
    assert f["folders_without_conversation"]["count"] == 0, f["folders_without_conversation"]
    assert f["conversations_without_messages"]["count"] == 0, f["conversations_without_messages"]
    assert f["unknown_entries"]["count"] == len(manifest.junk), f["unknown_entries"]


VARIANTS = {
    "full": ExportOptions(),
    "public_only": ExportOptions(tier="public_only"),
    "macos": ExportOptions(macos=True),
    "wrapper": ExportOptions(wrapper="Acme Slack export Jan 11 2026 - Jan 13 2026"),
    "macos_wrapper": ExportOptions(macos=True, wrapper="Acme export"),
    "zip64": ExportOptions(force_zip64=True),
    "data_descriptors": ExportOptions(data_descriptors=True),
    "zip64_data_descriptors": ExportOptions(force_zip64=True, data_descriptors=True),
    "utf8_flag": ExportOptions(non_ascii_names=True, names="utf8_flag"),
    "utf8_noflag": ExportOptions(non_ascii_names=True, names="utf8_noflag"),
    "cp437": ExportOptions(non_ascii_names=True, names="cp437"),
    "unicode_extra": ExportOptions(non_ascii_names=True, names="unicode_extra"),
    "no_directory_entries": ExportOptions(directory_entries=False),
}
REPORTED_ENCODING = {
    "utf8_noflag": "utf-8-unflagged",
    "cp437": "cp437",
    "unicode_extra": "utf-8-extra",
}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
async def test_variant_is_accepted_and_fully_accounted_for(
    exp: Api, tenant: TenantCtx, variant: str
) -> None:
    opts = VARIANTS[variant]
    data, manifest = generate(opts)
    out = await ingest(exp, tenant, data)
    assert_complete_mapping(out, manifest)
    f = out["findings"]
    assert out["detected_tier"] == ("public_only" if opts.tier == "public_only" else "full")
    assert out["root_prefix"] == f["root_prefix"] == (f"{opts.wrapper}/" if opts.wrapper else None)
    assert f["os_metadata_entries"]["count"] == len(manifest.junk)
    assert set(f["os_metadata_entries"]["sample"]) == set(manifest.junk)
    if variant in REPORTED_ENCODING:
        enc = f["name_encodings"]
        assert set(enc) == {REPORTED_ENCODING[variant]}, enc
        assert all(not n.isascii() for n in enc[REPORTED_ENCODING[variant]]["sample"])
    else:
        assert f["name_encodings"] == {}


async def test_ambiguous_names_are_reported_never_silently_mapped(
    exp: Api, tenant: TenantCtx
) -> None:
    """A CP437 folder name whose bytes also happen to be valid UTF-8 decodes to a different name. The
    export is accepted, and the mismatch shows up three ways: the name encoding, a folder no metadata
    file lists, and a listed conversation without messages."""
    cp437_name = "├⌐x"  # "├⌐x" in CP437 is the bytes C3 A9 78, which is UTF-8 "éx"
    buf = io.BytesIO()
    zw = ZipWriter(buf, names="cp437")
    zw.add("users.json", b"[]")
    zw.add("channels.json", json.dumps([{"id": "C1", "name": cp437_name}]).encode())
    zw.add(f"{cp437_name}/2026-01-05.json", b"[]")
    zw.close()
    out = await ingest(exp, tenant, buf.getvalue())
    f = out["findings"]
    assert out["status"] == "ready"
    assert f["name_encodings"] == {
        "utf-8-unflagged": {"count": 1, "sample": ["éx/2026-01-05.json"]}
    }
    assert f["folders_without_conversation"] == {"count": 1, "sample": ["éx"]}
    assert f["conversations_without_messages"] == {"count": 1, "sample": ["C1"]}


async def test_zip64_by_entry_count(exp_batched: Api, tenant: TenantCtx) -> None:
    """Over 65,535 entries: the end record overflows and the ZIP64 records carry the count.

    Its time on CI varied 4-6x across runners (ADR 0015 §24.20), so every phase is timed with what
    the host did meanwhile (`phase_timings`) and the worker's own phases are read from its log line."""

    async def wal() -> dict[str, int]:
        """The cluster's WAL counters (PostgreSQL 16 ``pg_stat_wal``): what a phase wrote."""
        conn = await superuser(exp_batched.settings)
        try:
            row = await conn.fetchrow(
                "SELECT wal_records, wal_fpi, wal_bytes::bigint AS wal_bytes, wal_write, wal_sync"
                " FROM pg_stat_wal"
            )
            # how the index tables were read: a plan that turned into sequential scans (stale
            # statistics mid-load) shows here as seq_tup_read in the millions
            tables = await conn.fetch(
                "SELECT relname, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch, n_live_tup,"
                " autoanalyze_count + analyze_count AS analyzes,"
                " autovacuum_count + vacuum_count AS vacuums FROM pg_stat_user_tables"
                " WHERE relname IN ('export_entries', 'export_day_files', 'export_threads',"
                " 'slack_exports')"
            )
        finally:
            await conn.close()
        assert row is not None
        out = {k: int(v) for k, v in row.items()}
        for t in tables:
            for k in TABLE_COUNTERS:
                out[f"{t['relname']}.{k}"] = int(t[k] or 0)
        return out

    async with phase_clock("test_export_variants::test_zip64_by_entry_count", wal) as clock:
        with captured_logs() as logs:
            async with clock.phase("generation"):
                data, manifest = await asyncio.to_thread(
                    generate,
                    ExportOptions(tier="public_only", padding_days=66_000, directory_entries=False),
                )
            assert manifest.entries > 65_535
            clock.extra["zip_bytes"] = len(data)
            async with exp_batched.client(
                tenant.subdomain, tenant.token(exp_batched.settings)
            ) as c:
                async with clock.phase("upload"):
                    export_id, done = await upload(c, tenant.default_client_id, data)
                    assert done.status_code == 202, done.text
                async with clock.phase("lock"):
                    await _left(c, export_id, "locking")
                sampler = WaitSampler(lambda: superuser(exp_batched.settings))
                explain = AutoExplain(
                    lambda: superuser(exp_batched.settings), _engine(exp_batched), sample_rate=0.05
                )
                try:
                    async with clock.phase("validation"), sampler.running(), explain.enabled():
                        out = await settled(c, export_id)
                finally:  # recorded whether validation settled or ran out of its bound
                    _record_validation(clock, exp_batched, sampler, explain)
            clock.extra["worker_phases"] = {
                e["event"]: {
                    k: e.get(k)
                    for k in (
                        "phases_ms",
                        "db_writes",
                        "range_requests",
                        "day_page_reads",
                        "day_page_read_ms",
                        "slowest_day_page_ms",
                    )
                }
                for e in logs.events()
                if e.get("event") in ("export locked", "export validated")
                and e.get("export_id") == export_id
            }
        # the test ends at `ready`: collecting from this export is not part of it (no phase to time)
        assert_complete_mapping(out, manifest)
        # the work bound, whatever the runner's speed (ADR 0015 §24.20): index tuples the
        # validation read from export_entries, a few passes over the export (entries, pages,
        # foreign-key checks, findings). A per-row check or per-page read that scans the export
        # again (stale statistics: 643 M on CI before the fix) multiplies it by thousands. The
        # counters may lag the last statements by the stats flush interval: only ever lower.
        series = exp_batched.export_ingests[-1].series
        assert series["analyze:export_entries"][0][0] < series["export_day_files"][0][0], (
            "the entries' statistics are refreshed before the first day-file row references them"
        )
        validation = next(p for p in clock.phases if p["phase"] == "validation")
        fetched = validation["counters"]["export_entries.idx_tup_fetch"]
        assert fetched <= 15 * manifest.entries, f"validation fetched {fetched} entry tuples"


def _record_validation(
    clock: PhaseClock, api: Api, sampler: WaitSampler, explain: AutoExplain
) -> None:
    if not api.export_ingests:
        return
    ingest = api.export_ingests[-1]  # the validating one (the lock activity made the one before)
    clock.extra["statement_series"] = {
        t: series_summary(v) for t, v in sorted(ingest.series.items())
    }
    clock.extra["raw_statement_series"] = ingest.series
    clock.extra["wait_events"] = sampler.record(ingest.started)
    plans = explain.record(ingest.started)
    clock.extra["raw_plan_examples"] = plans.pop("examples")
    clock.extra["plans"] = plans


TABLE_COUNTERS = (
    "seq_scan", "seq_tup_read", "idx_scan", "idx_tup_fetch", "n_live_tup", "analyzes", "vacuums",
)  # fmt: skip


def _engine(api: Api) -> AsyncEngine:
    bind = api.sessions.kw["bind"]
    assert isinstance(bind, AsyncEngine)
    return bind


async def _left(c: httpx.AsyncClient, export_id: str, status: str) -> None:
    """Until the export has left ``status`` (inside ``settled``'s own bound, which follows)."""
    async with asyncio.timeout(60):
        while True:
            if (await c.get(f"/v1/exports/{export_id}")).json()["status"] != status:
                return
            await asyncio.sleep(0.05)
