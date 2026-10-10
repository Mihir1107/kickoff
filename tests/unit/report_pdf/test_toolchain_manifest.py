"""The parts of the PDF toolchain id that need no report image (ADR 0018 §5.3, §5.6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from edisc_worker import versions


def test_zlib_is_on_the_layout_path() -> None:
    """zlib makes the compressed PDF streams (§5.6): a zlib change must change the toolchain id."""
    assert "zlib1g" in versions.LAYOUT_PACKAGES
    for pkg in ("libpango-1.0-0", "libharfbuzz-subset0", "libfreetype6", "fontconfig-config"):
        assert pkg in versions.LAYOUT_PACKAGES


def test_installers_are_not_part_of_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def dist(name: str, version: str) -> SimpleNamespace:
        return SimpleNamespace(metadata={"Name": name}, version=version)

    fake = [dist("WeasyPrint", "70.0"), dist("pip", "25.0"), dist("setuptools", "80"),
            dist("wheel", "0.45"), dist("pydyf", "0.12.1")]  # fmt: skip
    monkeypatch.setattr(versions.metadata, "distributions", lambda: fake)
    assert versions.python_packages() == {"pydyf": "0.12.1", "weasyprint": "70.0"}


def test_the_id_is_canonical_json_of_the_manifest() -> None:
    a = versions.toolchain_id({"b": 1, "a": {"y": 2, "x": 1}})
    assert a == versions.toolchain_id({"a": {"x": 1, "y": 2}, "b": 1}) and len(a) == 64
    assert a != versions.toolchain_id({"b": 2, "a": {"y": 2, "x": 1}})


def test_a_bundled_icc_unlike_the_vendored_profile_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "sRGB2014.icc"
    other.write_bytes(versions.vendored_icc()[:-1] + b"\0")
    monkeypatch.setattr(versions, "bundled_icc_path", lambda: other)
    with pytest.raises(versions.ToolchainError, match="differs"):
        versions.check_icc()
    other.write_bytes(versions.vendored_icc())
    assert len(versions.check_icc()) == 64


def test_no_report_image_means_no_toolchain_only_where_allowed() -> None:
    assert versions.report_toolchain_id(True, None) == versions.NO_TOOLCHAIN  # not an image here
    with pytest.raises(versions.ToolchainError):
        versions.report_toolchain_id(False, None)
