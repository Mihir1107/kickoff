"""A Slack export zip written from the dummy oracle (ADR 0014 M14.4).

Layout per Slack's documented export format: ``users.json``, ``channels.json`` and, for a full export,
``groups.json`` (private channels), ``dms.json`` and ``mpims.json``; one folder per conversation (the
channel name; the id for DMs *(confirm on real export)*) with one ``YYYY-MM-DD.json`` per day holding the
day's messages as a JSON array. Deleted messages are absent (exports carry no tombstones). Day files are
named by the UTC day of their messages here; real exports may use the workspace time zone, which is why
ingestion treats the file date as a hint only (R4).

Real-world variants (``ExportOptions``): a single wrapper folder, macOS re-zips (``__MACOSX/`` AppleDouble
files and ``.DS_Store``), ZIP64, streaming zips with data descriptors, and non-ASCII names written with
or without the UTF-8 flag. The returned ``ExportManifest`` is the oracle for ingestion tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, BinaryIO, Literal

from edisc_connector_dummy.dataset import Conversation, Dataset, ts_of
from edisc_connector_dummy.dialects.slack import message
from edisc_connector_dummy.zipwriter import DEFLATED, NameMode, ZipWriter

METADATA_FILE = {"channel": "channels.json", "private_channel": "groups.json",
                 "dm": "dms.json", "group_dm": "mpims.json"}  # fmt: skip
NON_ASCII = ("café", "größe", "año", "naïve")  # all representable in CP437 (the ZIP default)
APPLE_DOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\x00" * 58


@dataclass(frozen=True)
class ExportOptions:
    tier: Literal["public_only", "full"] = "full"
    epoch: int = 0
    wrapper: str | None = None  # e.g. "Acme Slack export Jan 5 2026 - Jan 8 2026"
    macos: bool = False  # __MACOSX/ AppleDouble twins and .DS_Store files
    force_zip64: bool = False
    data_descriptors: bool = False
    names: NameMode = "utf8_flag"
    non_ascii_names: bool = False  # channel names with non-ASCII letters
    directory_entries: bool = True  # "folder/" entries (confirm on real export)
    padding_days: int = 0  # extra empty day files in a "padding" channel (to pass 65,535 entries)
    method: int = DEFLATED


@dataclass
class ExportManifest:
    root: str | None
    entries: int = 0
    day_files: int = 0
    messages: int = 0
    metadata_files: list[str] = field(default_factory=list)
    conversations: dict[str, str] = field(default_factory=dict)  # folder -> conversation id
    junk: list[str] = field(default_factory=list)  # macOS entries: expected as unknown entries


def folder_name(conv: Conversation, opts: ExportOptions, index: int) -> str:
    if conv.kind == "dm":
        return conv.id
    if opts.non_ascii_names:
        return f"{NON_ASCII[index % len(NON_ASCII)]}-{index}"
    return conv.name


def _dump(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=4).encode("utf-8")


def _created(ds: Dataset) -> int:
    return int(ts_of(ds.day(0), 0).split(".")[0])


def write_export(ds: Dataset, out: BinaryIO, opts: ExportOptions | None = None) -> ExportManifest:
    opts = opts or ExportOptions()
    epoch = opts.epoch
    root = f"{opts.wrapper}/" if opts.wrapper else ""
    zw = ZipWriter(
        out,
        force_zip64=opts.force_zip64,
        data_descriptors=opts.data_descriptors,
        names=opts.names,
    )
    manifest = ExportManifest(root=root or None)

    def add(name: str, data: bytes = b"") -> None:
        zw.add(root + name, data, method=opts.method)
        manifest.entries += 1
        if opts.macos and not name.endswith("/"):
            parent, _, base = (root + name).rpartition("/")
            twin = f"__MACOSX/{parent + '/' if parent else ''}._{base}"
            zw.add(twin, APPLE_DOUBLE, method=opts.method)
            manifest.entries += 1
            manifest.junk.append(twin)

    if root and opts.directory_entries:
        zw.add(root)
        manifest.entries += 1
    if opts.macos:
        zw.add("__MACOSX/")
        manifest.entries += 1
        manifest.junk.append("__MACOSX/")
        zw.add(root + ".DS_Store", b"\x00\x00\x00\x01Bud1" + b"\x00" * 24)
        manifest.entries += 1
        manifest.junk.append(root + ".DS_Store")

    convs = [
        (i, c)
        for i, c in enumerate(ds.conversations())
        if opts.tier == "full" or c.kind == "channel"
    ]
    users = [_user(u) for u in ds.users(epoch)]
    add("users.json", _dump(users))
    manifest.metadata_files.append("users.json")
    by_file: dict[str, list[dict[str, Any]]] = {}
    for i, conv in convs:
        by_file.setdefault(METADATA_FILE[conv.kind], []).append(_conversation(ds, conv, opts, i))
    if opts.padding_days:
        by_file.setdefault("channels.json", []).append(
            {"id": "C99999PADDING", "name": "padding", "created": _created(ds), "creator": "",
             "is_archived": True, "is_general": False, "members": [],
             "topic": {"value": "", "creator": "", "last_set": 0},
             "purpose": {"value": "", "creator": "", "last_set": 0}}
        )  # fmt: skip
    for filename in ("channels.json", "groups.json", "dms.json", "mpims.json"):
        if filename in by_file:
            add(filename, _dump(by_file[filename]))
            manifest.metadata_files.append(filename)

    for i, conv in convs:
        folder = folder_name(conv, opts, i)
        manifest.conversations[folder] = conv.id
        if opts.directory_entries:
            zw.add(root + folder + "/")
            manifest.entries += 1
        for d in range(ds.n_days(epoch)):
            msgs = [m for m in ds.unit_messages(conv.id, d, epoch) if m.deleted_ts is None]
            if not msgs:
                continue
            add(
                f"{folder}/{ds.day(d).isoformat()}.json",
                _dump([message(ds, m, epoch) for m in msgs]),
            )
            manifest.day_files += 1
            manifest.messages += len(msgs)
    if opts.padding_days:
        manifest.conversations["padding"] = "C99999PADDING"
        if opts.directory_entries:
            zw.add(root + "padding/")
            manifest.entries += 1
        day = date(1900, 1, 1)
        for k in range(opts.padding_days):
            zw.add(
                f"{root}padding/{(day + timedelta(days=k)).isoformat()}.json",
                b"[]",
                method=opts.method,
            )
            manifest.entries += 1
            manifest.day_files += 1
    zw.close()
    return manifest


def _user(u: Any) -> dict[str, Any]:
    return {
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
    }


def _conversation(
    ds: Dataset, conv: Conversation, opts: ExportOptions, index: int
) -> dict[str, Any]:
    created = _created(ds)
    if conv.kind == "dm":
        return {"id": conv.id, "created": created, "members": list(conv.members)}
    out: dict[str, Any] = {
        "id": conv.id,
        "name": folder_name(conv, opts, index),
        "created": created,
        "creator": conv.members[0],
        "is_archived": False,
        "members": list(conv.members),
        "topic": {"value": "", "creator": "", "last_set": 0},
        "purpose": {"value": f"{conv.kind} {index}", "creator": "", "last_set": 0},
    }
    if conv.kind == "channel":
        out["is_general"] = index == 0
    return out
