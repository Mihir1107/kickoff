"""Real-world Slack export variants (M14.4), generated from the dummy oracle and ingested through the
real API and worker: macOS re-zips, a wrapper folder, ZIP64 (forced and by entry count), streaming zips
with data descriptors, and non-ASCII names with and without the UTF-8 flag. None is rejected; every
irregularity is reported, nothing is mangled silently."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportManifest, ExportOptions, write_export
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connector_dummy.zipwriter import ZipWriter

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
    """Over 65,535 entries: the end record overflows and the ZIP64 records carry the count."""
    data, manifest = generate(
        ExportOptions(tier="public_only", padding_days=66_000, directory_entries=False)
    )
    assert manifest.entries > 65_535
    out = await ingest(exp_batched, tenant, data)
    assert_complete_mapping(out, manifest)
