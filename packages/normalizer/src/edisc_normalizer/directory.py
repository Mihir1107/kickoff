"""Directory entries for the scope picker (M17 plan §3.3), from raw directory pages. PURE: bytes in,
entries out; the worker stores them (``edisc_worker.directory``), connectors only fetch the pages.

A page is a Slack ``users.list`` / ``conversations.list`` answer (API dialect) or an export's
``users.json`` (export dialect: the bare array). Each element gives one entry: its external id, a
display name, its kind (``user`` / ``conversation``), a finer type (``user``, ``bot``,
``public_channel``, ``private_channel``, ``im``, ``mpim``) and whether it is archived (deactivated,
for users). Names keep their exact text: a name holding a lone UTF-16 surrogate is stored as its
replacement text plus the exact code units (ADR 0018 §23); an id holding one cannot key an entry
and is listed under its stand-in key (``substitute_key``, §28), never dropped.

``search_key`` is what prefix search matches: the name (else the id) in NFKC case-folded form, so
``q`` is normalised the same way (:func:`search_key`).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from edisc_normalizer.slack import API, _conversation_type, _parse
from edisc_verify.lone_surrogates import has_lone, replacement, substitute_key, utf16_units

USER, CONVERSATION = "user", "conversation"
KINDS = (CONVERSATION, USER)
MAX_NAME = 1024  # longer names are cut (a picker label; the evidence keeps the full record)


@dataclass(frozen=True)
class DirectoryEntry:
    external_id: str
    name: str | None  # valid Unicode (replacement text for lone surrogates)
    name_units: list[int] | None  # the exact UTF-16 code units, when the name held a lone surrogate
    kind: str  # user | conversation
    entry_type: str
    archived: bool
    search_key: str


def search_key(text: str) -> str:
    """The prefix-search form of a name or a query: NFKC, case-folded, valid Unicode."""
    return unicodedata.normalize("NFKC", replacement(text)).casefold()[:MAX_NAME]


def _name(value: Any) -> tuple[str | None, list[int] | None]:
    if not isinstance(value, str) or not value:
        return None, None
    value = value[:MAX_NAME]
    if has_lone(value):
        return replacement(value), utf16_units(value)
    return value, None


def _key(value: str) -> str:
    return substitute_key(value) if has_lone(value) else value


def _user(raw: Mapping[str, Any]) -> DirectoryEntry | None:
    uid = raw.get("id")
    if not isinstance(uid, str) or not uid:
        return None
    found = raw.get("profile")
    profile: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    label = (
        profile.get("real_name") or raw.get("real_name") or profile.get("display_name")
        or raw.get("name")
    )  # fmt: skip
    name, units = _name(label)
    key = _key(uid)
    return DirectoryEntry(
        key, name, units, USER, "bot" if raw.get("is_bot") else "user", bool(raw.get("deleted")),
        search_key(name or key),
    )  # fmt: skip


def _conversation(raw: Mapping[str, Any]) -> DirectoryEntry | None:
    cid = raw.get("id")
    if not isinstance(cid, str) or not cid:
        return None
    kind = _conversation_type(raw)
    name, units = _name(raw.get("name") or (raw.get("user") if kind == "im" else None))
    key = _key(cid)
    return DirectoryEntry(
        key, name, units, CONVERSATION, kind, bool(raw.get("is_archived")), search_key(name or key)
    )


def entries(page: bytes, *, kind: str, dialect: str = API) -> list[DirectoryEntry]:
    """Every entry of one directory page of ``kind`` (``users.list`` members or
    ``conversations.list`` channels; an export's ``users.json``). Elements without an id are not
    entries (they could not be picked); malformed pages raise ``NormalizationError``."""
    if kind not in KINDS:
        raise ValueError(f"unknown directory kind {kind!r}")
    field, build = ("members", _user) if kind == USER else ("channels", _conversation)
    return [e for raw in _parse(page, field, dialect) if (e := build(raw)) is not None]


# an export's conversation kinds (`export_conversations.kind`) as directory entry types
EXPORT_TYPES = {"channel": "public_channel", "group": "private_channel", "dm": "im", "mpim": "mpim"}


def export_conversation_entry(
    conversation_id: str, name: str | None, kind: str, name_units: list[int] | None
) -> DirectoryEntry:
    """An export's conversation, from its validated ``export_conversations`` row (no source
    call): the id as stored (a stand-in key when the export's id held a lone surrogate, §28), the
    name as stored (valid text) with the exact code units the row recorded."""
    label = name[:MAX_NAME] if name else None
    return DirectoryEntry(
        conversation_id, label, name_units if label is not None else None, CONVERSATION,
        EXPORT_TYPES.get(kind, kind), False, search_key(label or conversation_id),
    )  # fmt: skip
