"""Byte-identity inputs besides the data are pinned and recorded (ADR 0015 §6)."""

from __future__ import annotations

import pathlib
import re
import shutil
import sys
import tomllib
import unicodedata
import zoneinfo
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import metadata, resources

import pytest

from edisc_renderers.rsmf import (
    TZDATA_VERSION,
    UNICODE_VERSION,
    RenderInputError,
    RenderOptions,
    golden_key,
    load_zone,
    runtime_versions,
)
from edisc_renderers.rsmf.version import RENDERER_VERSION

ROOT = pathlib.Path(__file__).parents[3]
WINTER = datetime(2026, 1, 15, 12, tzinfo=UTC)


@pytest.fixture
def poisoned_system_zoneinfo(tmp_path: pathlib.Path) -> Iterator[None]:
    """A system zoneinfo where America/New_York is really UTC."""
    fake = tmp_path / "America"
    fake.mkdir()
    utc_file = resources.files("tzdata.zoneinfo").joinpath("UTC")
    with utc_file.open("rb") as src, (fake / "New_York").open("wb") as dst:
        shutil.copyfileobj(src, dst)
    zoneinfo.reset_tzpath(to=[str(tmp_path)])
    zoneinfo.ZoneInfo.clear_cache()
    load_zone.cache_clear()
    try:
        yield
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()
        load_zone.cache_clear()


@pytest.mark.usefixtures("poisoned_system_zoneinfo")
def test_zones_come_from_the_tzdata_package_not_the_system() -> None:
    # the poisoned system path is really in effect for the stdlib lookup ...
    assert zoneinfo.ZoneInfo("America/New_York").utcoffset(WINTER).total_seconds() == 0
    # ... and the renderer ignores it
    zone = RenderOptions(time_zone="America/New_York").zone()
    assert zone.utcoffset(WINTER).total_seconds() == -5 * 3600


def test_zone_names_are_checked() -> None:
    assert load_zone("UTC") is UTC
    for bad in ("../../etc/passwd", "America//New_York", "", "Mars/Olympus", "America/./X"):
        with pytest.raises(RenderInputError):
            RenderOptions(time_zone=bad)


def test_python_is_pinned_exactly() -> None:
    pinned = (ROOT / ".python-version").read_text().strip()
    assert re.fullmatch(r"3\.12\.\d+", pinned), "pin the full patch version"
    assert ".".join(map(str, sys.version_info[:3])) == pinned
    assert unicodedata.unidata_version == UNICODE_VERSION


def test_tzdata_is_pinned_exactly() -> None:
    deps = tomllib.loads((ROOT / "packages/renderers/pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    (pin,) = [d for d in deps if d.startswith("tzdata")]
    assert pin == f"tzdata=={metadata.version('tzdata')}"
    assert re.fullmatch(r"\d{4}[a-z]", TZDATA_VERSION)


def test_versions_are_in_the_summary_and_the_golden_key() -> None:
    assert runtime_versions() == {
        "renderer_version": RENDERER_VERSION,
        "unicode_version": UNICODE_VERSION,
        "tzdata_version": TZDATA_VERSION,
    }
    assert golden_key() == f"{RENDERER_VERSION}_unicode-{UNICODE_VERSION}_tzdata-{TZDATA_VERSION}"
