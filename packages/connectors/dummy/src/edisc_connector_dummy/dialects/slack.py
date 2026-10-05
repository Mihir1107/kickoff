"""Slack-shaped raw payloads (conversations.history / conversations.replies / users.list).

Shape follows Slack's Web API closely enough that the normalizer exercises realistic structure: string
``ts``, ``thread_ts``, ``edited``, ``files``, ``reactions``, ``subtype``, ``user_profile`` embeds,
``blocks``, and volatile fields that must NOT define a version (reply counters, expiring file URLs,
profile embeds). Volatile values vary with the epoch only, so output stays byte-identical per epoch.

Deleted messages are returned as tombstones (``subtype: message_deleted``), like Discovery-style APIs.
"""

from __future__ import annotations

import json
from typing import Any

from edisc_connector_dummy.dataset import SYSTEM_SUBTYPES, Dataset, Msg, User, h64


def _profile(ds: Dataset, user: User, day_index: int, epoch: int) -> dict[str, Any]:
    index = int(user.id[1:6])
    display = ds.display_name_at(index, day_index, epoch)
    return {
        "avatar_hash": f"{h64(ds.seed, 'avatar', index, display):016x}"[:12],
        "real_name": user.real_name,
        "display_name": display,
        "team": user.team_id,
        "is_restricted": False,
    }


def message(ds: Dataset, m: Msg, epoch: int) -> dict[str, Any]:
    users = {u.id: u for u in ds.users(epoch)}
    author = users[m.user]
    if m.deleted_ts is not None:
        out: dict[str, Any] = {
            "type": "message",
            "subtype": "message_deleted",
            "user": m.user,
            "text": "",
            "ts": m.ts,
            "deleted_ts": m.deleted_ts,
            "hidden": True,
        }
        if m.thread_ts:
            out["thread_ts"] = m.thread_ts
        return out
    out = {"type": "message"}
    if m.subtype:
        out["subtype"] = m.subtype
    out["user"] = m.user
    out["text"] = m.text
    out["ts"] = m.ts
    out["team"] = author.team_id
    if author.team_id != ds.spec.workspace_id:
        out["user_team"] = author.team_id  # shared-channel (external) author
    if author.bot_id:
        out["bot_id"] = author.bot_id
        if author.is_app_user:
            out["app_id"] = f"A{author.id[1:6]}DUMMY"
    if m.subtype not in SYSTEM_SUBTYPES:
        out["client_msg_id"] = (
            f"{h64(ds.seed, 'cmid', m.conversation_id, m.ts):016x}-0000-4000-8000-000000000000"
        )
        out["user_profile"] = _profile(ds, author, m.day_index, epoch)
        out["blocks"] = [
            {
                "type": "rich_text",
                "block_id": f"{h64(ds.seed, 'blk', m.conversation_id, m.ts, m.text):016x}"[:6],
                "elements": [
                    {"type": "rich_text_section", "elements": [{"type": "text", "text": m.text}]}
                ],
            }
        ]
    if m.thread_ts:
        out["thread_ts"] = m.thread_ts
        if m.is_parent:
            # volatile: changes whenever a reply arrives, never a new version of the parent
            out["reply_count"] = len(m.reply_ts)
            reply_users = sorted({r.user for r in ds.thread(m.conversation_id, m.ts, epoch)[1:]})
            out["reply_users_count"] = len(reply_users)
            out["reply_users"] = reply_users
            if m.reply_ts:
                out["latest_reply"] = m.reply_ts[-1]
            out["is_locked"] = False
            out["subscribed"] = False
        else:
            parent = ds.thread(m.conversation_id, m.thread_ts, epoch)[:1]
            out["parent_user_id"] = next(p.user for p in parent)
            if m.subtype == "thread_broadcast" and parent:
                # Slack embeds the thread root in a broadcast; volatile (its counters move with
                # every reply), never part of the broadcast's own version
                out["root"] = {
                    "text": parent[0].text if parent[0].deleted_ts is None else "",
                    "user": parent[0].user,
                    "ts": parent[0].ts,
                    "thread_ts": parent[0].ts,
                    "reply_count": len(parent[0].reply_ts),
                }
    if m.edited:
        out["edited"] = {"user": m.edited[0], "ts": m.edited[1]}
    if m.files:
        out["files"] = [
            {
                "id": f.id,
                "created": int(m.ts.split(".")[0]),
                "name": f.name,
                "title": f.name,
                "mimetype": f.mimetype,
                "filetype": f.filetype,
                "user": m.user,
                "size": f.size,
                "mode": "hosted",
                "is_external": False,
                # volatile: expiring, per-collection URL tokens
                "url_private": f"https://files.dummy.test/{f.id}/{f.name}?t={h64(ds.seed, 'url', f.id, epoch):016x}",
                "url_private_download": f"https://files.dummy.test/{f.id}/download/{f.name}?t={h64(ds.seed, 'url', f.id, epoch):016x}",
                "permalink": f"https://dummy.test/files/{m.user}/{f.id}/{f.name}",
            }
            for f in m.files
        ]
        out["upload"] = True
    if m.reactions:
        out["reactions"] = [
            {"name": name, "users": list(who), "count": len(who)} for name, who in m.reactions
        ]
    return out


def _dump(payload: dict[str, Any]) -> bytes:
    # Raw, not canonical: key order is construction order, like a real API response.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def history_page(
    ds: Dataset, messages: list[Msg], epoch: int, *, has_more: bool, next_cursor: str | None
) -> bytes:
    return _dump(
        {
            "ok": True,
            "messages": [message(ds, m, epoch) for m in messages],
            "has_more": has_more,
            "pin_count": 0,
            "channel_actions_ts": None,
            "channel_actions_count": 0,
            "response_metadata": {"next_cursor": next_cursor or ""},
        }
    )


def replies_page(
    ds: Dataset, messages: list[Msg], epoch: int, *, has_more: bool, next_cursor: str | None
) -> bytes:
    return _dump(
        {
            "ok": True,
            "messages": [message(ds, m, epoch) for m in messages],
            "has_more": has_more,
            "response_metadata": {"next_cursor": next_cursor or ""},
        }
    )


def users_page(ds: Dataset, users: list[User], epoch: int, *, next_cursor: str | None) -> bytes:
    members = []
    for u in users:
        member: dict[str, Any] = {
            "id": u.id,
            "team_id": u.team_id,
            "name": u.name,
            "deleted": u.deleted,
            "real_name": u.real_name,
            "profile": {
                "display_name": u.display_name,
                "real_name": u.real_name,
                "avatar_hash": u.avatar_hash,
                "title": "",
                **({"email": u.email} if u.email else {}),
                **({"bot_id": u.bot_id} if u.bot_id else {}),
            },
            "is_bot": u.is_bot,
            "is_app_user": u.is_app_user,
            "updated": int(ds.epoch_time(epoch).split(".")[0]),
        }
        if u.is_stranger:
            member["is_stranger"] = True
        members.append(member)
    return _dump(
        {
            "ok": True,
            "members": members,
            "cache_ts": int(ds.epoch_time(epoch).split(".")[0]),
            "response_metadata": {"next_cursor": next_cursor or ""},
        }
    )


def conversations_page(
    ds: Dataset, conversation_ids: list[str], epoch: int, *, next_cursor: str | None
) -> bytes:
    """``conversations.list`` with each conversation's members inlined (the live connector will call
    ``conversations.members``; the dummy folds it in). Topic/purpose carry creator and last_set,
    which are hints, not content."""
    channels = []
    for cid in conversation_ids:
        st = ds.conversation_state(cid, epoch)
        kind = st["kind"]
        when = int(ds.epoch_time(epoch).split(".")[0])
        channels.append(
            {
                "id": cid,
                "name": st["name"],
                "is_channel": kind == "channel",
                "is_group": kind == "private_channel",
                "is_im": kind == "dm",
                "is_mpim": kind == "group_dm",
                "is_private": kind != "channel",
                "is_archived": st["archived"],
                "is_shared": st["shared"],
                "is_ext_shared": st["shared"],
                "topic": {"value": st["topic"], "creator": "", "last_set": when},
                "purpose": {"value": st["purpose"], "creator": "", "last_set": when},
                "members": st["members"],
                "num_members": len(st["members"]),  # type: ignore[arg-type]
                "updated": when,
            }
        )
    return _dump(
        {"ok": True, "channels": channels, "response_metadata": {"next_cursor": next_cursor or ""}}
    )
