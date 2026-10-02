"""Slack export layout: classification, conversation records, tier detection (ADR 0014 sections 3-5)."""

from __future__ import annotations

from datetime import date

import pytest

from edisc_connector_slack_export.layout import (
    BLIND_SPOTS_PUBLIC_ONLY,
    Placement,
    classify,
    conversation_record,
    detect_tier,
    nested_metadata,
)


@pytest.mark.parametrize(
    ("name", "is_dir", "expected"),
    [
        ("users.json", False, Placement("metadata")),
        ("something_new.json", False, Placement("metadata")),
        ("README.txt", False, Placement("unknown")),
        ("general/", True, Placement("directory")),
        ("general/2026-01-05.json", False, Placement("day", "general", date(2026, 1, 5))),
        ("D0123/2026-12-31.json", False, Placement("day", "D0123", date(2026, 12, 31))),
        ("general/2026-02-30.json", False, Placement("unknown")),  # not a date
        ("general/2026-1-5.json", False, Placement("unknown")),
        ("general/notes.json", False, Placement("unknown")),
        ("a/b/2026-01-05.json", False, Placement("unknown")),
        ("ws1/channels.json", False, Placement("unknown")),
    ],
)
def test_classify(name: str, is_dir: bool, expected: Placement) -> None:
    assert classify(name, is_dir) == expected


def test_nested_metadata_marks_per_workspace_layout() -> None:
    assert nested_metadata("ws1/channels.json") and nested_metadata("ws1/users.json")
    assert not nested_metadata("general/2026-01-05.json") and not nested_metadata("channels.json")


def test_conversation_records() -> None:
    assert conversation_record("channel", {"id": "C1", "name": "general"}).folder == "general"  # type: ignore[union-attr]
    assert conversation_record("dm", {"id": "D1"}).folder == "D1"  # type: ignore[union-attr]
    assert conversation_record("mpim", {"id": "G1", "name": "mpdm-a--b-1"}).folder == "mpdm-a--b-1"  # type: ignore[union-attr]
    for bad in (
        [],
        {"name": "general"},
        {"id": "", "name": "x"},
        {"id": "C1"},
        {"id": "C1", "name": "a/b"},
        {"id": "C1", "name": ".."},
        {"id": "C1", "name": 5},
    ):
        assert conversation_record("channel", bad) is None, bad


def test_tier_detection() -> None:
    public = detect_tier(
        {"users.json", "channels.json"}, nested_metadata_seen=False, declared_plan=None
    )
    assert public is not None and (public.tier, public.confirmed) == ("public_only", True)
    assert set(BLIND_SPOTS_PUBLIC_ONLY) <= set(public.blind_spots) and not public.warnings

    full = detect_tier(
        {"users.json", "channels.json", "dms.json"}, nested_metadata_seen=False, declared_plan="pro"
    )
    assert full is not None and full.tier == "full" and full.confirmed
    assert any("Declared plan pro" in w for w in full.warnings)
    assert not set(BLIND_SPOTS_PUBLIC_ONLY) & set(full.blind_spots)

    grid = detect_tier(
        {"channels.json", "org_users.json"}, nested_metadata_seen=False, declared_plan=None
    )
    assert grid is not None and (grid.tier, grid.confirmed) == ("grid", False)
    assert any("users.json is missing" in w for w in grid.warnings)
    nested = detect_tier(
        {"channels.json"}, nested_metadata_seen=True, declared_plan="enterprise_grid"
    )
    assert nested is not None and nested.tier == "grid"

    downgraded = detect_tier(
        {"users.json", "channels.json"}, nested_metadata_seen=False, declared_plan="business_plus"
    )
    assert downgraded is not None and any("public channels only" in w for w in downgraded.warnings)
    assert detect_tier({"users.json"}, nested_metadata_seen=False, declared_plan=None) is None
