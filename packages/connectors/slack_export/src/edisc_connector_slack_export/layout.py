"""The Slack export layout (ADR 0014 sections 3, 4, 5): entry classification, conversation metadata and
tier detection. Pure functions over entry names and parsed metadata records; no I/O.

Documented layout: top-level ``*.json`` metadata files, one folder per conversation holding one
``YYYY-MM-DD.json`` file per day. Anything else is an **unknown entry**: kept in the evidence, listed in
the report, never processed and never silently dropped (R2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

KNOWN_METADATA = frozenset(
    {
        "users.json",
        "channels.json",
        "groups.json",
        "dms.json",
        "mpims.json",
        "integration_logs.json",
        "canvases.json",  # (confirm on real export)
        "org_users.json",  # Enterprise Grid org-level users (confirm on real export)
    }
)
# conversation metadata file -> conversation kind
CONVERSATION_FILES = {
    "channels.json": "channel",
    "groups.json": "group",
    "dms.json": "dm",
    "mpims.json": "mpim",
}
FULL_MARKERS = frozenset({"groups.json", "dms.json", "mpims.json"})
GRID_MARKERS = frozenset({"org_users.json"})  # (confirm on real export)

_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.json$")

BLIND_SPOTS_ALL = (
    "Edits keep only the latest text; earlier versions are not in the export.",
    "Deleted messages are absent (no tombstones).",
    "History may be limited by the plan's visible history or the workspace retention settings; the "
    "export's date range was chosen by whoever ran it.",
    "File contents are not in the export: only links, downloaded separately.",
    "Canvases, lists, huddle transcripts and Clips are absent unless their files are present.",
    "Shared-channel content from other organisations may be partial.",
)
BLIND_SPOTS_PUBLIC_ONLY = (
    "Private channels, direct messages and group direct messages are not in this export.",
)
PLAN_TIERS = {  # the tiers an admin's declared plan normally produces
    "free": {"public_only"},
    "pro": {"public_only"},
    "business_plus": {"public_only", "full"},
    "enterprise_grid": {"public_only", "full", "grid"},
}


@dataclass(frozen=True)
class Placement:
    kind: str  # metadata | day | directory | unknown
    folder: str | None = None
    hint_day: date | None = None


def classify(name: str, is_dir: bool) -> Placement:
    """Where an entry sits in the documented layout. Names were already validated by the archive
    reader (no absolute paths, ``..``, empty segments or backslashes)."""
    parts = name.rstrip("/").split("/")
    if is_dir:
        return Placement("directory")
    if len(parts) == 1:
        return Placement("metadata") if name.endswith(".json") else Placement("unknown")
    if len(parts) == 2:
        m = _DAY.match(parts[1])
        if m is not None:
            try:
                day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return Placement("unknown")
            return Placement("day", parts[0], day)
    return Placement("unknown")


def nested_metadata(name: str) -> bool:
    """``<workspace>/channels.json`` and similar: the per-workspace layout of an org-level export."""
    parts = name.split("/")
    return len(parts) == 2 and parts[1] in {*CONVERSATION_FILES, "users.json"}


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    kind: str
    folder: str
    name: str | None


def conversation_record(kind: str, element: Any) -> ConversationRecord | None:
    """One element of a conversation metadata file, or None when it lacks what is needed to find its
    folder (counted and reported by the caller). Channels, private channels and group DMs are stored
    under their ``name``; DMs under their id *(confirm on real export)*."""
    if not isinstance(element, dict):
        return None
    cid, name = element.get("id"), element.get("name")
    if not isinstance(cid, str) or not cid:
        return None
    if name is not None and not isinstance(name, str):
        return None
    folder = cid if kind == "dm" else name
    if not folder or "/" in folder or folder in (".", ".."):
        return None
    return ConversationRecord(cid, kind, folder, name)


@dataclass(frozen=True)
class Tier:
    tier: str  # public_only | full | grid
    confirmed: bool
    blind_spots: tuple[str, ...]
    warnings: tuple[str, ...]


def detect_tier(
    metadata: set[str], *, nested_metadata_seen: bool, declared_plan: str | None
) -> Tier | None:
    """The tier from the archive alone (ADR 0014 section 5), or None when this is not a Slack export
    (no conversation metadata file at all). A Grid-looking archive is processed as ``full`` and flagged
    as unconfirmed until real exports confirm the markers."""
    if not metadata & CONVERSATION_FILES.keys():
        return None
    warnings: list[str] = []
    if metadata & GRID_MARKERS or nested_metadata_seen:
        tier, confirmed = "grid", False
        warnings.append(
            "Enterprise Grid structure suspected: processed as a full export; tier unconfirmed."
        )
    elif metadata & FULL_MARKERS:
        tier, confirmed = "full", True
    else:
        tier, confirmed = "public_only", True
    if "users.json" not in metadata:
        warnings.append("users.json is missing: authors cannot be resolved to user profiles.")
    if declared_plan is not None and tier not in PLAN_TIERS[declared_plan]:
        warnings.append(f"Declared plan {declared_plan} does not normally produce a {tier} export.")
    elif declared_plan in ("business_plus", "enterprise_grid") and tier == "public_only":
        warnings.append(
            f"Declared plan {declared_plan} but the export holds public channels only: private "
            "channels and direct messages are not in it."
        )
    spots = BLIND_SPOTS_ALL + (BLIND_SPOTS_PUBLIC_ONLY if tier == "public_only" else ())
    return Tier(tier, confirmed, spots, tuple(warnings))
