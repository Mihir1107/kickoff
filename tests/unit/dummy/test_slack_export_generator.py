"""The synthetic Slack export (M14.4): every variant is a valid zip for both ``zipfile`` and our hardened
reader, names round-trip exactly, and the manifest (the oracle) matches the archive."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportOptions, write_export
from edisc_connector_dummy.spec import DatasetSpec
from edisc_custody.archive import ArchiveLimits, BytesSource, NameEncoding, read_entry, scan

SPEC = DatasetSpec(seed=7, conversations=4, days=2, messages_per_unit=12)
LIMITS = ArchiveLimits(read_chunk=1 << 16)

VARIANTS = {
    "plain": ExportOptions(),
    "public_only": ExportOptions(tier="public_only"),
    "wrapper": ExportOptions(wrapper="Acme Slack export Jan 5 2026 - Jan 7 2026"),
    "macos": ExportOptions(macos=True),
    "macos_wrapper": ExportOptions(macos=True, wrapper="Acme export"),
    "zip64": ExportOptions(force_zip64=True),
    "descriptors": ExportOptions(data_descriptors=True),
    "zip64_descriptors": ExportOptions(force_zip64=True, data_descriptors=True),
    "utf8_flag": ExportOptions(non_ascii_names=True, names="utf8_flag"),
    "utf8_noflag": ExportOptions(non_ascii_names=True, names="utf8_noflag"),
    "cp437": ExportOptions(non_ascii_names=True, names="cp437"),
    "unicode_extra": ExportOptions(non_ascii_names=True, names="unicode_extra"),
    "stored": ExportOptions(method=zipfile.ZIP_STORED),
    "no_dirs": ExportOptions(directory_entries=False),
}
EXPECTED_ENCODING = {
    "utf8_flag": NameEncoding.UTF8,
    "utf8_noflag": NameEncoding.UTF8_UNFLAGGED,
    "cp437": NameEncoding.CP437,
    "unicode_extra": NameEncoding.UTF8_EXTRA,
}


def build(opts: ExportOptions) -> tuple[bytes, object]:
    buf = io.BytesIO()
    manifest = write_export(Dataset(SPEC), buf, opts)
    return buf.getvalue(), manifest


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_every_variant_is_a_valid_zip_with_exact_names(variant: str) -> None:
    opts = VARIANTS[variant]
    data, manifest = build(opts)
    assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None  # an independent reader agrees
    src = BytesSource(data)
    entries = asyncio.run(scan(src, LIMITS))
    assert len(entries) == manifest.entries  # type: ignore[attr-defined]
    root = manifest.root or ""  # type: ignore[attr-defined]
    names = {e.name for e in entries}
    for folder in manifest.conversations:  # type: ignore[attr-defined]
        assert any(n.startswith(f"{root}{folder}/") for n in names), folder  # exact round trip
    if variant in EXPECTED_ENCODING:
        non_ascii = [e for e in entries if not e.name.isascii()]
        assert non_ascii and {e.name_encoding for e in non_ascii} == {EXPECTED_ENCODING[variant]}
    channels = next(e for e in entries if e.name == f"{root}channels.json")
    body, _ = asyncio.run(read_entry(src, channels, LIMITS))
    listed = {c["name"] for c in json.loads(body)}
    assert listed <= set(manifest.conversations)  # type: ignore[attr-defined]


def test_tiers_and_zip64_by_entry_count() -> None:
    _, full = build(ExportOptions())
    _, public = build(ExportOptions(tier="public_only"))
    assert {"groups.json", "dms.json", "mpims.json"} <= set(full.metadata_files)  # type: ignore[attr-defined]
    assert public.metadata_files == ["users.json", "channels.json"]  # type: ignore[attr-defined]
    data, big = build(
        ExportOptions(tier="public_only", padding_days=66_000, directory_entries=False)
    )
    assert big.entries > 65_535  # type: ignore[attr-defined]
    assert data[-22:][:4] == b"PK\x05\x06" and data[-42:-38] == b"PK\x06\x07"  # zip64 locator
    assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None


def test_output_is_deterministic() -> None:
    assert build(VARIANTS["macos_wrapper"])[0] == build(VARIANTS["macos_wrapper"])[0]
