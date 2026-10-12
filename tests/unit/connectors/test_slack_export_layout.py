"""Slack export layout: classification, conversation records, tier detection (ADR 0014 sections 3-5)."""

from __future__ import annotations

from datetime import date

import pytest

from edisc_connector_slack_export.layout import (
    BLIND_SPOTS_PUBLIC_ONLY,
    Placement,
    RootDetector,
    classify,
    conversation_record,
    detect_tier,
    is_junk,
    nested_metadata,
    relative,
    storable_key,
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


@pytest.mark.parametrize(
    ("element", "team"),
    [
        ({"id": "C1", "name": "g"}, None),
        ({"id": "C1", "name": "g", "context_team_id": "T2", "team_id": "T3"}, "T2"),
        ({"id": "C1", "name": "g", "team_id": "T3", "team": "T4"}, "T3"),
        ({"id": "C1", "name": "g", "team": "E1"}, "E1"),
        ({"id": "C1", "name": "g", "team": {"id": "T5"}}, None),  # not a team id
        ({"id": "C1", "name": "g", "context_team_id": "", "team": "T6"}, "T6"),
        ({"id": "C1", "name": "g", "team_id": "T/../x"}, None),  # never a path segment
    ],
)
def test_conversation_team(element: dict[str, object], team: str | None) -> None:
    rec = conversation_record("channel", element)
    assert rec is not None and rec.team_id == team


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


def _root(names: list[str]) -> str | None:
    d = RootDetector()
    for n in names:
        d.feed(n, n.endswith("/"))
    return d.root()


def test_wrapper_folder_detection() -> None:
    w = "Acme Slack export Jan 1 2026"
    assert _root([f"{w}/", f"{w}/channels.json", f"{w}/general/2026-01-05.json"]) == f"{w}/"
    # macOS litter does not hide a wrapper
    assert (
        _root([f"{w}/channels.json", "__MACOSX/", f"__MACOSX/{w}/._channels.json", ".DS_Store"])
        == f"{w}/"
    )
    assert _root(["channels.json", "general/2026-01-05.json"]) is None  # plain export
    assert _root(["general/2026-01-05.json", "general/2026-01-06.json"]) is None  # a lone folder
    assert _root([f"{w}/channels.json", "other/channels.json"]) is None  # two roots
    assert relative(f"{w}/general/x.json", f"{w}/") == "general/x.json"
    assert relative("elsewhere.json", f"{w}/") is None
    assert relative("a.json", None) == "a.json"


def test_os_litter() -> None:
    for junk in (
        "__MACOSX/",
        "__MACOSX/general/._2026-01-05.json",
        ".DS_Store",
        "general/.DS_Store",
        "Thumbs.db",
    ):
        assert is_junk(junk), junk
    assert not is_junk("general/2026-01-05.json") and not is_junk("MACOSX.json")


def test_a_name_with_a_lone_surrogate_is_kept_exactly_never_refused() -> None:
    """Names are text (ADR 0018 §28): stored as the replacement text with the exact code units; the
    folder is what a UTF-8 writer makes of the name (U+FFFD per surrogate). Nothing is refused."""
    lone = chr(0xD83D)
    rec = conversation_record("channel", {"id": "C1", "name": "gen" + lone})
    assert rec is not None and rec.refused is None and rec.conversation_id == "C1"
    assert rec.name == rec.folder == "gen\ufffd"
    assert rec.exact == {"/name": [ord("g"), ord("e"), ord("n"), 0xD83D]}
    # a real U+FFFD in another channel's name gives the same folder: a duplicate the findings name
    other = conversation_record("channel", {"id": "C2", "name": "gen\ufffd"})
    assert other is not None and other.folder == rec.folder and other.exact == {}


def test_an_id_or_team_with_a_lone_surrogate_refuses_the_conversation_under_a_stand_in() -> None:
    """Structural values (ADR 0018 §28): the conversation is listed under a stand-in id holding the
    exact code units, and refused with the reason, so its units become gaps; never dropped."""
    lone = chr(0xDC00)
    rec = conversation_record("channel", {"id": "C" + lone, "name": "general"})
    assert rec is not None and rec.refused == "conversation_id_lone_surrogate"
    assert rec.conversation_id == "lone-surrogate:0043.dc00" and rec.folder == "general"
    assert rec.exact == {"/id": [0x43, 0xDC00]}
    dm = conversation_record("dm", {"id": "D" + lone})  # a DM's folder is its id
    assert dm is not None and dm.folder == "D\ufffd" and dm.refused is not None
    team = conversation_record("channel", {"id": "C1", "name": "general", "team_id": "T" + lone})
    assert team is not None and team.refused == "team_id_lone_surrogate" and team.team_id is None
    assert team.exact == {"/team_id": [0x54, 0xDC00]} and team.conversation_id == "C1"
    # the first team field present decides; a later valid one never stands in for it
    both = {"id": "C1", "name": "g", "context_team_id": "T" + lone, "team_id": "T1"}
    assert conversation_record("channel", both).refused == "team_id_lone_surrogate"  # type: ignore[union-attr]
    # the export's workspace id holds one: conversations without a team of their own are refused
    ws = conversation_record("channel", {"id": "C1", "name": "g"}, workspace_unkeyable=True)
    assert ws is not None and ws.refused == "workspace_id_lone_surrogate"
    own = {"id": "C1", "name": "g", "team_id": "T1"}
    assert conversation_record("channel", own, workspace_unkeyable=True).refused is None  # type: ignore[union-attr]


def test_a_key_with_a_lone_surrogate_is_not_storable() -> None:
    assert storable_key("1767571200.000100") == "1767571200.000100"
    assert storable_key("1767571200." + chr(0xD800)) is None
    assert storable_key(None) is None and storable_key(5) is None
