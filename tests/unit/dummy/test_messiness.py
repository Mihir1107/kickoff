"""Pagination, time, content and identity messiness: each case is guaranteed to exist in the dataset."""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime, time

import pytest

from edisc_connector_dummy.dataset import Dataset, ts_to_datetime
from edisc_connectors_base.types import BatchKind

from .conftest import batches, connection, connector, full_scope, make_spec, messages, units


async def _history(spec_overrides: dict[str, object] | None = None, epoch: int = 0):  # type: ignore[no-untyped-def]
    spec = make_spec(**(spec_overrides or {}))
    c, _ = connector()
    conn, scope = connection(spec, epoch), full_scope(spec, epoch)
    out = {}
    for u in await units(c, conn, scope):
        out[u] = [b for b in await batches(c, conn, u, scope) if b.kind is BatchKind.HISTORY]
    return spec, c, conn, out


# ------------------------------------------------------------------ pagination
async def test_empty_page_with_has_more_true() -> None:
    _, _, _, pages = await _history()
    for unit_pages in pages.values():
        empties = [p for p in unit_pages if not messages(p)]
        assert len(empties) == 1
        assert json.loads(empties[0].body)["has_more"] is True
        assert empties[0].next_cursor is not None  # an empty page is NOT the end


async def test_pages_overlap_and_items_are_out_of_order() -> None:
    _, _, _, pages = await _history()
    overlaps = out_of_order = 0
    for unit_pages in pages.values():
        seen: set[str] = set()
        previous = None
        for p in unit_pages:
            for m in messages(p):
                overlaps += m["ts"] in seen
                seen.add(m["ts"])
                if (
                    previous is not None
                    and float(m["ts"]) < float(previous)
                    and m["ts"] not in seen - {m["ts"]}
                ):
                    out_of_order += 1
                previous = m["ts"]
    assert overlaps > 0
    assert out_of_order > 0


async def test_a_thread_is_split_across_page_boundaries() -> None:
    _, _, _, pages = await _history({"page_size": 5})
    split = 0
    for unit_pages in pages.values():
        where: dict[str, set[int]] = {}
        for index, p in enumerate(unit_pages):
            for m in messages(p):
                if "thread_ts" in m:
                    where.setdefault(m["thread_ts"], set()).add(index)
        split += sum(1 for idx in where.values() if len(idx) > 1)
    assert split > 0


async def test_clean_pagination_can_be_requested() -> None:
    _, _, _, pages = await _history({"messy_pagination": False})
    for unit_pages in pages.values():
        ts = [m["ts"] for p in unit_pages for m in messages(p)]
        assert ts == sorted(ts, key=float)
        assert len(ts) == len(set(ts))


# ------------------------------------------------------------------ time
async def test_every_unit_has_messages_exactly_on_both_slice_boundaries() -> None:
    spec, _, _, pages = await _history()
    for u, unit_pages in pages.items():
        instants = {ts_to_datetime(m["ts"]) for p in unit_pages for m in messages(p)}
        assert datetime.combine(u.day, time(0, 0, 0), tzinfo=UTC) in instants
        assert datetime.combine(u.day, time(23, 59, 59, 999000), tzinfo=UTC) in instants
        assert all(i.date() == u.day for i in instants)  # nothing leaks into a neighbouring day


async def test_epoch_edits_are_timestamped_after_the_collection_window() -> None:
    spec, _, _, pages = await _history(epoch=1)
    ds = Dataset(spec)
    window_end = datetime.combine(
        ds.day(spec.days), time(0), tzinfo=UTC
    )  # end of the epoch-0 window
    late = [
        m
        for unit_pages in pages.values()
        for p in unit_pages
        for m in messages(p)
        if "edited" in m
        and ts_to_datetime(m["edited"]["ts"]) >= window_end
        and ts_to_datetime(m["ts"]) < window_end
    ]
    assert late, "epoch 1 must edit messages that were inside the original window"


# ------------------------------------------------------------------ content
async def test_messy_content_is_present_and_byte_exact() -> None:
    spec, _, _, pages = await _history()
    texts = [m["text"] for unit_pages in pages.values() for p in unit_pages for m in messages(p)]
    joined = "\n".join(texts)
    assert "\U0001f469‍\U0001f469‍\U0001f467‍\U0001f466" in joined  # ZWJ emoji sequence
    assert "\U0001f1ee\U0001f1f3" in joined  # flag (regional indicators)
    assert "שלום" in joined and "مرحبا" in joined  # Hebrew + Arabic (RTL)
    assert "​" in joined and "‌" in joined and "﻿" in joined  # zero-width characters
    # combining sequence and its precomposed twin are BOTH present and NOT normalized into each other
    assert "café" in joined and "café" in joined
    assert unicodedata.normalize("NFC", "café") == "café"
    assert max(len(t) for t in texts) >= 40_000  # very long message
    attachment_only = [
        m
        for unit_pages in pages.values()
        for p in unit_pages
        for m in messages(p)
        if m["text"] == "" and m.get("files") and m.get("subtype") != "message_deleted"
    ]
    assert attachment_only, "a message with an empty body and only an attachment"


async def test_files_are_fetchable_deterministic_and_shared_between_messages() -> None:
    spec, c, conn, pages = await _history()
    uses: dict[str, int] = {}
    sizes: dict[str, int] = {}
    for unit_pages in pages.values():
        for ts_files in {
            (m["ts"], f["id"], f["size"])
            for p in unit_pages
            for m in messages(p)
            for f in m.get("files", [])
        }:
            uses[ts_files[1]] = uses.get(ts_files[1], 0) + 1
            sizes[ts_files[1]] = ts_files[2]
    assert max(uses.values()) > 1, "the same file attached to several messages (dedup case)"
    file_id = next(iter(sizes))
    first = b"".join([chunk async for chunk in c.open_file(conn, file_id)])
    second = b"".join([chunk async for chunk in c.open_file(conn, file_id)])
    assert first == second
    assert len(first) == sizes[file_id]


# ------------------------------------------------------------------ identity
async def test_directory_has_deactivated_external_bot_and_app_users() -> None:
    spec, c, conn, _ = await _history()
    members = [
        m
        for b in [b async for b in c.fetch_directory(conn, None)]
        if b.request["method"] == "users.list"
        for m in json.loads(b.body)["members"]
    ]
    assert len(members) == spec.users
    assert any(m["deleted"] for m in members)
    assert any(m.get("is_stranger") and m["team_id"] != spec.workspace_id for m in members)
    assert any(m["is_bot"] and not m["is_app_user"] for m in members)
    assert any(m["is_bot"] and m["is_app_user"] for m in members)


async def test_special_identities_author_messages() -> None:
    spec, c, conn, pages = await _history()
    all_msgs = [m for unit_pages in pages.values() for p in unit_pages for m in messages(p)]
    authors = {m["user"] for m in all_msgs}
    assert {f"U{i:05d}DUMMY" for i in range(5)} <= authors
    assert any(m.get("subtype") == "bot_message" and "bot_id" in m for m in all_msgs)
    assert any(m.get("user_team") == "T0EXTERNAL" for m in all_msgs)
    assert any(m.get("subtype") == "channel_join" for m in all_msgs)  # system messages


async def test_user_renamed_mid_dataset_and_between_epochs() -> None:
    spec, c, conn, pages = await _history({"conversations": 1, "days": 4})
    names = {
        (u.day, m["user_profile"]["display_name"])
        for u, unit_pages in pages.items()
        for p in unit_pages
        for m in messages(p)
        if m["user"] == "U00000DUMMY" and "user_profile" in m
    }
    assert {n for _, n in names} == {"Alice", "Alice (renamed)"}  # same user id, two display names

    async def directory(epoch: int) -> dict[str, str]:
        cc, _ = connector()
        bs = [b async for b in cc.fetch_directory(connection(spec, epoch), None)]
        return {
            m["id"]: m["profile"]["display_name"]
            for b in bs
            if b.request["method"] == "users.list"
            for m in json.loads(b.body)["members"]
        }

    before, after = await directory(0), await directory(1)
    assert before["U00005DUMMY"] != after["U00005DUMMY"]
    assert {k for k in before if before[k] != after[k]} == {"U00005DUMMY"}


@pytest.mark.parametrize("epoch", [0, 1, 2])
async def test_directory_has_versioned_conversation_metadata(epoch: int) -> None:
    """conversations.list pages follow the users: conversation 0 is renamed at epoch 1, conversation 1
    archived from epoch 2; every conversation of the dataset is listed with its members."""
    spec = make_spec()
    c, _ = connector()
    conn = connection(spec, epoch)
    channels = {
        ch["id"]: ch
        for b in [b async for b in c.fetch_directory(conn, None)]
        if b.request["method"] == "conversations.list"
        for ch in json.loads(b.body)["channels"]
    }
    ds = Dataset(spec)
    assert set(channels) == {cv.id for cv in ds.conversations()}
    first, second = ds.conversations()[0], ds.conversations()[1]
    assert channels[first.id]["name"] == (first.name + "-renamed" if epoch >= 1 else first.name)
    assert channels[second.id]["is_archived"] is (epoch >= 2)
    assert all(ch["members"] == list(ds.conversation(cid).members) for cid, ch in channels.items())
    kinds = {cid: ds.conversation(cid).kind for cid in channels}
    assert set(kinds.values()) == {"channel", "private_channel", "dm", "group_dm"}
    assert all(ch["is_im"] == (kinds[cid] == "dm") for cid, ch in channels.items())
    assert all(ch["is_mpim"] == (kinds[cid] == "group_dm") for cid, ch in channels.items())
