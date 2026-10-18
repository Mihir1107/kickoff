"""Directory entries from raw pages (``edisc_normalizer.directory``, M17 plan §3.3): pure."""

from __future__ import annotations

import json

import pytest

from edisc_core.lone_surrogates import substitute_key, utf16_units
from edisc_normalizer import directory as d
from edisc_normalizer.slack import NormalizationError


def _page(field: str, elements: list[dict[str, object]]) -> bytes:
    return json.dumps({"ok": True, field: elements}).encode()


def test_users_take_the_real_name_their_type_and_deactivation() -> None:
    page = _page("members", [
        {"id": "U1", "name": "dana", "real_name": "Dana Q", "profile": {"real_name": "Dana Quinn",
         "display_name": "dq"}},
        {"id": "U2", "name": "bot", "is_bot": True, "profile": {}},
        {"id": "U3", "name": "gone", "deleted": True, "profile": {"display_name": "Gone"}},
        {"name": "no id"},
    ])  # fmt: skip
    got = {e.external_id: e for e in d.entries(page, kind=d.USER)}
    assert set(got) == {"U1", "U2", "U3"}  # an element without an id is not an entry
    assert (got["U1"].name, got["U1"].entry_type, got["U1"].archived) == (
        "Dana Quinn",
        "user",
        False,
    )
    assert (got["U2"].name, got["U2"].entry_type) == ("bot", "bot")
    assert (got["U3"].name, got["U3"].archived) == ("Gone", True)
    assert all(e.kind == d.USER and e.name_units is None for e in got.values())


def test_conversations_state_their_type_and_archive() -> None:
    page = _page("channels", [
        {"id": "C1", "name": "general"}, {"id": "G1", "name": "secret", "is_private": True},
        {"id": "D1", "is_im": True, "user": "U9"}, {"id": "G2", "name": "mpdm-a", "is_mpim": True},
        {"id": "C2", "name": "old", "is_archived": True},
    ])  # fmt: skip
    got = {
        e.external_id: (e.name, e.entry_type, e.archived)
        for e in d.entries(page, kind=d.CONVERSATION)
    }
    assert got == {
        "C1": ("general", "public_channel", False), "G1": ("secret", "private_channel", False),
        "D1": ("U9", "im", False), "G2": ("mpdm-a", "mpim", False), "C2": ("old", "public_channel", True),
    }  # fmt: skip


def test_an_export_users_file_is_the_bare_array() -> None:
    page = json.dumps([{"id": "U1", "real_name": "Ann"}]).encode()
    (e,) = d.entries(page, kind=d.USER, dialect="export")
    assert (e.external_id, e.name) == ("U1", "Ann")
    with pytest.raises(NormalizationError):
        d.entries(page, kind=d.USER)  # the API dialect needs {"ok": true, "members": [...]}


def test_a_lone_surrogate_name_is_replaced_with_its_code_units_and_an_id_is_a_stand_in() -> None:
    """ADR 0018 §23, §28: escaped JSON can carry a lone surrogate; nothing is dropped."""
    lone = "Ann " + chr(0xD83D)
    page = (b'{"ok": true, "members": [{"id": "U1", "real_name": "Ann \\ud83d"},'
            b' {"id": "U\\udc00", "real_name": "Bo"}]}')  # fmt: skip
    first, second = d.entries(page, kind=d.USER)
    assert first.name == "Ann �" and first.name_units == utf16_units(lone)
    assert second.external_id == substitute_key("U" + chr(0xDC00)) and second.name == "Bo"


def test_the_search_key_is_nfkc_case_folded() -> None:
    assert d.search_key("Ｕｓｅｒ ＡＢ") == d.search_key("user ab") == "user ab"  # noqa: RUF001
    assert d.search_key("Straße") == "strasse"
    page = _page("members", [{"id": "U1", "real_name": "ＤＡＮＡ"}])  # noqa: RUF001
    assert d.entries(page, kind=d.USER)[0].search_key == "dana"
    nameless = _page("channels", [{"id": "D7", "is_im": True}])
    assert d.entries(nameless, kind=d.CONVERSATION)[0].search_key == "d7"  # falls back to the id


def test_unknown_kinds_and_malformed_pages_raise() -> None:
    with pytest.raises(ValueError, match="unknown directory kind"):
        d.entries(b"{}", kind="group")
    with pytest.raises(NormalizationError):
        d.entries(b"{not json", kind=d.USER)
    with pytest.raises(NormalizationError):
        d.entries(json.dumps({"ok": False, "error": "invalid_auth"}).encode(), kind=d.USER)


def test_export_conversations_keep_their_stored_names_and_units() -> None:
    e = d.export_conversation_entry("C1", "general �", "group", [103, 0xD800])
    assert (e.entry_type, e.name, e.name_units, e.archived) == (
        "private_channel", "general �", [103, 0xD800], False)  # fmt: skip
    assert d.export_conversation_entry("D1", None, "dm", None).search_key == "d1"
