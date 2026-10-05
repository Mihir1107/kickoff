"""The pure RSMF renderer (ADR 0015): slice inputs in, deterministic `.rsmf` files out.

`render_slice` renders one conversation-day; the worker loader calls it slice by slice and feeds a
`Reconciler`. `render_job` does the same for a whole in-memory job (tests and small jobs).
Bytes are produced lazily by `RenderedFile.stream(opener)`, so file evidence is never held whole.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any

from edisc_core.canonical import canonical_json
from edisc_core.schemas import ARCHIVE_CAVEAT
from edisc_core.time import format_utc, from_epoch
from edisc_custody.merkle import batch_root
from edisc_renderers.rsmf import eml
from edisc_renderers.rsmf.model import (
    AsyncFileOpener,
    ConversationInfo,
    FileAttachment,
    FileOpener,
    FileOutcome,
    FileUnavailable,
    Identity,
    JobInfo,
    Message,
    RenderInputError,
    RenderOptions,
    SliceInput,
)
from edisc_renderers.rsmf.names import MANIFEST_NAME, attachment_name, placeholder_name
from edisc_renderers.rsmf.reconcile import Reconciler, Reconciliation, subject_digest
from edisc_renderers.rsmf.slicing import event_order, slice_bounds, slice_day, split_parts
from edisc_renderers.rsmf.validate import check_structure, validate_manifest
from edisc_renderers.rsmf.version import RENDERER_VERSION, RSMF_VERSION
from edisc_renderers.rsmf.zipstream import ZipEntry, as_async, azip_stream, check_limits, drive

_EVENT_COLLECTION_NS = uuid.uuid5(uuid.NAMESPACE_URL, "urn:edisc:rsmf:eventcollectionid")
_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")
_JOIN_LEAVE = {"channel_join": "join", "group_join": "join", "channel_leave": "leave", "group_leave": "leave"}  # fmt: skip
# subtypes that are ordinary messages; any other subtype is rendered as `unknown`, never dropped
_MESSAGE_SUBTYPES = frozenset(
    {None, "bot_message", "me_message", "thread_broadcast", "file_share", "message_deleted",
     "tombstone", "reply_broadcast"}
)  # fmt: skip
_CONTEXT_OUTSIDE_FILE = "thread_root_outside_file"
_CONTEXT_OUT_OF_SCOPE = "thread_root_out_of_scope"
_NOT_RENDERED_EXCLUDED = "context_excluded"
_NOT_RENDERED_NOT_COLLECTED = "not_collected"
_KIND = {"im": "dm", "mpim": "mpim", "public_channel": "public", "private_channel": "private"}
_RSMF_TYPE = {"im": "direct", "mpim": "direct", "public_channel": "channel", "private_channel": "channel"}  # fmt: skip


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _custom(pairs: Iterable[tuple[str, str | None]]) -> list[dict[str, str]]:
    """Name/value pairs, empty values omitted (the schema needs a non-empty value), sorted."""
    return [{"name": n, "value": v} for n, v in sorted((n, v) for n, v in pairs if v)]


def _root_of(message: Message) -> str | None:
    root = message.current.thread_root
    return root if root and root != message.ts else None


@dataclass(frozen=True)
class _Placed:
    """A message as an event of one file."""

    message: Message
    parent: str | None
    context: str | None = None  # edisc.context marker for context events
    not_rendered: tuple[str, str] | None = None  # (root ts, reason) when the parent is omitted


@dataclass(frozen=True)
class RenderedFile:
    """One `.rsmf` file: everything but the evidence bytes is computed and validated already."""

    name: str
    conversation_id: str
    day: date
    time_zone: str
    part: int
    parts: int
    event_count: int
    context_event_count: int
    attachment_count: int  # zip entries other than the manifest (files and placeholders)
    unavailable_count: int  # placeholder entries
    source_hash: str
    event_collection_id: str
    manifest: bytes  # canonical JSON, exactly as in the zip
    headers: tuple[tuple[str, str], ...]
    summary: str
    entries: tuple[ZipEntry, ...]

    @property
    def boundary(self) -> str:
        return f"rsmf-{self.source_hash[:40]}"

    def astream(self, opener: AsyncFileOpener) -> AsyncIterator[bytes]:
        """The EML bytes in chunks. Evidence is streamed through `opener` and verified as it passes;
        a mismatch raises mid-stream, and the consumer must discard what it wrote (the production
        writer aborts the upload, so no object is created)."""
        return eml.aenvelope(
            self.headers, self.boundary, self.summary, azip_stream(self.entries, opener)
        )

    def stream(self, opener: FileOpener) -> Iterator[bytes]:
        """`astream` for synchronous callers (outside an event loop) with a synchronous opener."""
        return drive(self.astream(as_async(opener)))

    def record(self) -> dict[str, Any]:
        """Payload for the render's `rsmf_rendered` custody event (the writer adds SHA-256 and size)."""
        return {
            "name": self.name,
            "conversation_id": self.conversation_id,
            "day": self.day.isoformat(),
            "time_zone": self.time_zone,
            "part": self.part,
            "parts": self.parts,
            "source_hash": self.source_hash,
            "event_collection_id": self.event_collection_id,
            "event_count": self.event_count,
            "context_event_count": self.context_event_count,
            "attachment_count": self.attachment_count,
            "unavailable_count": self.unavailable_count,
        }


# ------------------------------------------------------------------ participants
def _identity_at(snapshots: Sequence[Identity], end: datetime) -> Identity | None:
    """The snapshot in force at the end of the slice (the latest one effective before it)."""
    ordered = sorted(
        snapshots, key=lambda s: (s.effective_from is not None, s.effective_from or end)
    )
    chosen: Identity | None = None
    for s in ordered:
        if s.effective_from is None or s.effective_from < end:
            chosen = s
    return chosen


def _display(user_id: str, identity: Identity | None) -> str:
    if identity is not None:
        for candidate in (identity.display_name, identity.real_name):
            if candidate and candidate.strip():
                return candidate
    return user_id


def _known_names(snapshots: Sequence[Identity]) -> list[tuple[str, str]]:
    """Every name a participant had in any snapshot, so an old name stays searchable."""
    names = {n for s in snapshots for n in (s.display_name, s.real_name) if n and n.strip()}
    return [("edisc.known_name", n) for n in sorted(names)]


def _participant(
    user_id: str, identity: Identity | None, snapshots: Sequence[Identity]
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": user_id,
        "account_id": user_id,
        "display": _display(user_id, identity),
    }
    pairs: list[tuple[str, str | None]] = [("slack.user_id", user_id), *_known_names(snapshots)]
    if identity is not None:
        if identity.email and _EMAIL.match(identity.email):
            out["email"] = identity.email
        pairs += [
                ("slack.team_id", identity.team_id),
                ("slack.is_bot", None if identity.is_bot is None else _flag(identity.is_bot)),
                ("slack.is_app_user", None if identity.is_app_user is None else _flag(identity.is_app_user)),
                ("slack.deactivated", None if identity.deactivated is None else _flag(identity.deactivated)),
        ]  # fmt: skip
    out["custom"] = _custom(pairs)
    return out


# ------------------------------------------------------------------ events
def _edit_time(ts: str | None) -> str | None:
    return format_utc(from_epoch(ts)) if ts else None


def _event(
    placed: _Placed,
    conversation_id: str,
    files: Mapping[str, FileOutcome],
    zip_files: dict[str, FileOutcome],
) -> dict[str, Any]:
    """The manifest event; also registers its zip entries in `zip_files`."""
    m, cur = placed.message, placed.message.current
    subtype = cur.subtype
    etype = _JOIN_LEAVE.get(subtype or "", "message" if subtype in _MESSAGE_SUBTYPES else "unknown")
    event: dict[str, Any] = {
        "id": m.ts,
        "type": etype,
        "participant": cur.author,
        "conversation": conversation_id,
        "timestamp": format_utc(m.sent_at),
    }
    if cur.deleted:
        event["deleted"] = True
    elif cur.text:
        event["body"] = cur.text
    if placed.parent is not None:
        event["parent"] = placed.parent

    # reactions recorded before a deletion are history, like the earlier text in `edits`: a deleted
    # event carries no RSMF `reactions` (which would say they are there now), only custom
    # `edisc.reactions_before_deletion` entries, "<name> (<count>): <users>"
    historical: list[tuple[str, str | None]] = []
    if m.reactions is not None and m.reactions.reactions:
        if cur.deleted:
            historical = [
                (
                    "edisc.reactions_before_deletion",
                    f"{name} ({len(users)}): {','.join(sorted(users))}",
                )
                for name, users in sorted(m.reactions.reactions)
            ]
        else:
            reactions = []
            for name, users in sorted(m.reactions.reactions):
                r: dict[str, Any] = {"value": name, "count": len(users)}
                if users:
                    r["participants"] = sorted(users)
                reactions.append(r)
            event["reactions"] = reactions

    edits = []
    for prev, new in zip(m.states, m.states[1:], strict=False):
        when = new.deleted_ts if new.deleted and new.deleted_ts else None
        if when is None and new.edited_ts and new.edited_ts != prev.edited_ts:
            when = new.edited_ts
        edit: dict[str, Any] = {"participant": new.author, "previous": prev.text, "new": new.text}
        stamp = _edit_time(when)
        if stamp is not None:
            edit["timestamp"] = stamp
        edits.append(edit)
    if edits:
        event["edits"] = edits

    attachments = []
    unavailable: list[tuple[str, str | None]] = []
    for fid in [] if cur.deleted else cur.file_ids:
        outcome = files.get(fid)
        if outcome is None:
            raise RenderInputError(f"{m.subject}: no outcome for file {fid}")
        if outcome.file_id != fid:
            raise RenderInputError(f"file map key {fid} holds file {outcome.file_id}")
        if isinstance(outcome, FileAttachment):
            zname = attachment_name(fid, outcome.name)
            attachments.append({"id": zname, "display": outcome.name or fid, "size": outcome.size})
        else:
            zname = placeholder_name(fid, outcome.name)
            text = _placeholder_text(outcome)
            attachments.append({"id": zname, "display": outcome.name or fid, "size": len(text)})
            unavailable.append(("edisc.file_unavailable", f"{fid}: {outcome.reason}"))
        known = zip_files.setdefault(zname, outcome)
        if known.file_id != fid:
            raise RenderInputError(f"zip name collision: {zname} for {known.file_id} and {fid}")
    if attachments:
        event["attachments"] = attachments

    pairs: list[tuple[str, str | None]] = [
        ("edisc.idempotency_key", cur.item.idempotency_key),
        ("edisc.content_hash", cur.item.content_hash),
        ("edisc.source_item_id", cur.item.source_item_id),
        ("edisc.version", str(cur.item.version)),
        ("edisc.in_scope", _flag(m.in_scope)),
        ("edisc.prior_version_keys", ",".join(s.item.idempotency_key for s in m.states[:-1])),
        ("edisc.context", placed.context),
        ("slack.subtype", subtype),
        *unavailable,
        *historical,
    ]
    if m.reactions is not None:
        pairs += [
            ("edisc.reactions.idempotency_key", m.reactions.item.idempotency_key),
            ("edisc.reactions.content_hash", m.reactions.item.content_hash),
        ]
    if placed.not_rendered is not None:
        pairs += [
            ("edisc.parent_not_rendered", placed.not_rendered[0]),
            ("edisc.parent_not_rendered_reason", placed.not_rendered[1]),
        ]
    event["custom"] = _custom(pairs)
    return event


def _placeholder_text(f: FileUnavailable) -> bytes:
    lines = [
        "This file was not collected. This text file stands in for it.",
        f"File: {attachment_name(f.file_id, f.name)}",
        f"File id: {f.file_id}",
        f"Name in the message: {f.name}",
        f"Reason reported by the source: {f.reason}",
        f"Recorded as item: {f.item.idempotency_key}",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


def _leaves(message: Message) -> Iterator[tuple[str, str]]:
    for s in message.states:
        yield s.item.idempotency_key, s.item.content_hash
    if message.reactions is not None:
        yield message.reactions.item.idempotency_key, message.reactions.item.content_hash


# ------------------------------------------------------------------ one file
def _build_file(
    inp: SliceInput,
    options: RenderOptions,
    bounds: tuple[datetime, datetime],
    part_no: int,
    part_count: int,
    part: Sequence[Message],
    lookup: Mapping[str, Message],
) -> RenderedFile:
    conv = inp.conversation
    primary_ts = {m.ts for m in part}
    placed: list[_Placed] = []
    context: dict[str, _Placed] = {}
    for m in part:
        root = _root_of(m)
        if root is None or root in primary_ts:
            placed.append(_Placed(m, root))
            continue
        root_msg = lookup.get(root)
        if options.include_context and root_msg is not None:
            marker = _CONTEXT_OUTSIDE_FILE if root_msg.in_scope else _CONTEXT_OUT_OF_SCOPE
            context.setdefault(root, _Placed(root_msg, None, context=marker))
            placed.append(_Placed(m, root))
        else:
            reason = _NOT_RENDERED_EXCLUDED if root_msg is not None else _NOT_RENDERED_NOT_COLLECTED
            placed.append(_Placed(m, None, not_rendered=(root, reason)))
    everything = sorted([*placed, *context.values()], key=lambda p: event_order(p.message))
    if len(everything) > options.cap:
        raise AssertionError("part over the cap after planning")

    zip_files: dict[str, FileOutcome] = {}
    events = [_event(p, conv.id, inp.files, zip_files) for p in everything]

    leaves: dict[str, str] = {}

    def leaf(key: str, content: str) -> None:
        if leaves.setdefault(key, content) != content:
            raise RenderInputError(f"item {key} with two content hashes")

    for p in everything:
        for key, content in _leaves(p.message):
            leaf(key, content)
    for outcome in zip_files.values():
        leaf(outcome.item.idempotency_key, outcome.item.content_hash)
    source_hash = batch_root(leaves.items())

    # participants: everyone an event, reaction or edit references, plus members and the custodian
    observed: set[str] = set()
    for e in events:
        observed.add(e["participant"])
        observed.update(u for r in e.get("reactions", []) for u in r.get("participants", []))
        observed.update(  # people who reacted before a deletion are still referenced
            u
            for pair in e.get("custom", [])
            if pair["name"] == "edisc.reactions_before_deletion"
            for u in pair["value"].split(": ", 1)[1].split(",")
            if u
        )
        observed.update(x["participant"] for x in e.get("edits", []))
    members = sorted(set(conv.members)) if conv.members else sorted(observed)
    everyone = observed | set(members) | set(conv.custodians)
    identities = {u: _identity_at(inp.identities.get(u, ()), bounds[1]) for u in everyone}
    participants = [
        _participant(u, identities[u], inp.identities.get(u, ())) for u in sorted(everyone)
    ]
    displays = {p["id"]: p["display"] for p in participants}

    conversation: dict[str, Any] = {
        "id": conv.id,
        "platform": "slack",
        "participants": members,
        "custom": _custom(
            [
                ("slack.conversation_type", conv.slack_type),
                ("slack.kind", _KIND[conv.slack_type] if conv.slack_type else None),
                # the RSMF type is optional: omitted, and said so, when the source gave no metadata
                ("edisc.conversation_metadata", None if conv.slack_type else "not_collected"),
                ("slack.workspace_id", conv.workspace_id),
                ("slack.is_shared", None if conv.is_shared is None else _flag(conv.is_shared)),
                (
                    "slack.is_ext_shared",
                    None if conv.is_ext_shared is None else _flag(conv.is_ext_shared),
                ),
                ("slack.is_archived", None if conv.archived is None else _flag(conv.archived)),
                ("slack.topic", conv.topic),
                ("slack.purpose", conv.purpose),
                *(("edisc.known_name", n) for n in conv.known_names),
                # every custodian covering the conversation; the RSMF field only when there is one
                *(("edisc.custodian", c) for c in conv.custodians),
            ]
        ),
    }
    if conv.slack_type is not None:
        conversation["type"] = _RSMF_TYPE[conv.slack_type]
    if conv.name:
        conversation["display"] = conv.name
    if len(conv.custodians) == 1:
        conversation["custodian"] = conv.custodians[0]

    slice_id = f"{inp.job.job_id}/{conv.id}/{inp.day.isoformat()}/{part_no}"
    collection_id = str(uuid.uuid5(_EVENT_COLLECTION_NS, slice_id))
    manifest: dict[str, Any] = {
        "version": RSMF_VERSION,
        "eventcollectionid": collection_id,
        "participants": participants,
        "conversations": [conversation],
        "events": events,
    }
    manifest_bytes = canonical_json(manifest)

    entries = [ZipEntry(MANIFEST_NAME, data=manifest_bytes)]
    for zname, outcome in zip_files.items():
        if isinstance(outcome, FileAttachment):
            entries.append(ZipEntry(zname, file=outcome))
        else:
            entries.append(ZipEntry(zname, data=_placeholder_text(outcome)))
    entries.sort(key=lambda e: e.name.encode("utf-8"))
    check_limits(entries)
    validate_manifest(json.loads(manifest_bytes))
    check_structure(json.loads(manifest_bytes), [e.name for e in entries])

    begin, end = everything[0].message.sent_at, everything[-1].message.sent_at
    unavailable = sum(isinstance(o, FileUnavailable) for o in zip_files.values())
    custodian = ", ".join(displays[c] for c in conv.custodians) or None
    title = conv.name or conv.id
    headers: list[tuple[str, str]] = [
        ("Date", eml.rfc5322_date(end)),
        ("From", "rsmf@rsmf.edisc"),
        ("Subject", f"Slack {title} {inp.day.isoformat()} ({options.time_zone}) part {part_no} of {part_count}"),
        ("Message-ID", f"<{source_hash}@rsmf.edisc>"),
        ("X-RSMF-Version", RSMF_VERSION),
        ("X-RSMF-Generator", f"edisc-renderers/{RENDERER_VERSION}"),
        ("X-RSMF-RendererVersion", RENDERER_VERSION),
        ("X-RSMF-Application", "Slack"),
        ("X-RSMF-BeginDate", format_utc(begin)),
        ("X-RSMF-EndDate", format_utc(end)),
        ("X-RSMF-EventCount", str(len(events))),
        ("X-RSMF-AttachmentCount", str(len(entries) - 1)),
        *([("X-RSMF-Custodian", custodian)] if custodian else []),
        ("X-RSMF-Participants", ", ".join(p["display"] for p in participants)),
        ("X-RSMF-EventCollectionID", collection_id),
        ("X-RSMF-CollectionId", str(inp.job.job_id)),
        ("X-RSMF-ConnectorVersion", inp.job.connector_version),
        ("X-RSMF-NormalizerVersion", inp.job.normalizer_version),
        ("X-RSMF-IncludeContext", _flag(options.include_context)),
        ("X-RSMF-SourceHash", source_hash),
        ("X-RSMF-Slice", f"conversation={conv.id}; day={inp.day.isoformat()}; tz={options.time_zone}"),
        ("X-RSMF-Part", f"{part_no}/{part_count}"),
        ("X-RSMF-CompletenessBasis", inp.job.completeness_basis),
    ]  # fmt: skip

    summary_lines = [
        f"Slack conversation {title} ({conv.id}), {inp.day.isoformat()} ({options.time_zone}), "
        f"part {part_no} of {part_count}.",
        f"Events: {len(events)} ({len(context)} thread context). Attachments: {len(entries) - 1} "
        f"({unavailable} unavailable, shown as placeholders).",
        f"Collection job {inp.job.job_id}; completeness basis: {inp.job.completeness_basis}.",
        f"Rendered by edisc-renderers/{RENDERER_VERSION}; include_context={_flag(options.include_context)}.",
    ]
    if inp.job.completeness_basis == "archive":
        summary_lines += ["", ARCHIVE_CAVEAT]
    return RenderedFile(
        name=f"{conv.id}_{inp.day.isoformat()}_part{part_no:03d}of{part_count:03d}.rsmf",
        conversation_id=conv.id,
        day=inp.day,
        time_zone=options.time_zone,
        part=part_no,
        parts=part_count,
        event_count=len(events),
        context_event_count=len(context),
        attachment_count=len(entries) - 1,
        unavailable_count=unavailable,
        source_hash=source_hash,
        event_collection_id=collection_id,
        manifest=manifest_bytes,
        headers=tuple(headers),
        summary="\r\n".join(summary_lines) + "\r\n",
        entries=tuple(entries),
    )


# ------------------------------------------------------------------ one slice
def render_slice(inp: SliceInput, options: RenderOptions) -> list[RenderedFile]:
    """Render one conversation-day. An empty slice produces no file. Raises on inconsistent input."""
    conv = inp.conversation
    if not _CONVERSATION_ID.match(conv.id):
        raise RenderInputError(f"unsafe conversation id {conv.id!r}")
    zone = options.zone()
    bounds = slice_bounds(inp.day, zone)
    by_ts: dict[str, Message] = {}
    for m in inp.messages:
        if not m.in_scope:
            raise RenderInputError(f"{m.subject}: out-of-scope items are only rendered as context")
        _check_message(m, conv.id)
        if not bounds[0] <= m.sent_at < bounds[1]:
            raise RenderInputError(f"{m.subject} is outside slice {conv.id}/{inp.day}")
        if m.ts in by_ts:
            raise RenderInputError(f"{m.subject} given twice")
        by_ts[m.ts] = m
    if not by_ts:
        return []

    lookup: dict[str, Message] = dict(by_ts)
    for ts, given in inp.roots.items():
        _check_message(given, conv.id)
        if given.ts != ts or _root_of(given) is not None:
            raise RenderInputError(f"{given.subject} is not a thread root under key {ts}")
        lookup.setdefault(ts, given)
    for m in by_ts.values():
        root = _root_of(m)
        if root is not None and root not in lookup and root not in inp.missing_roots:
            raise RenderInputError(
                f"{m.subject}: root {root} neither provided nor declared missing"
            )
        if root is not None and root in lookup and _root_of(lookup[root]) is not None:
            raise RenderInputError(f"{m.subject}: root {root} is itself a reply")

    def context_root(m: Message) -> str | None:
        root = _root_of(m)
        if root is None or not options.include_context or root not in lookup:
            return None
        return root

    primaries = sorted(by_ts.values(), key=event_order)
    parts = split_parts(primaries, context_root, options.cap)
    return [
        _build_file(inp, options, bounds, i + 1, len(parts), part, lookup)
        for i, part in enumerate(parts)
    ]


def _check_message(m: Message, conversation_id: str) -> None:
    if m.conversation_id != conversation_id:
        raise RenderInputError(f"{m.subject} belongs to {m.conversation_id}, not {conversation_id}")
    if from_epoch(m.ts) != m.sent_at:
        raise RenderInputError(f"{m.subject}: sent_at does not match ts {m.ts}")


# ------------------------------------------------------------------ a whole job in memory
@dataclass(frozen=True)
class RenderResult:
    files: tuple[RenderedFile, ...]
    reconciliation: Reconciliation


def render_job(
    job: JobInfo,
    conversations: Sequence[ConversationInfo],
    messages: Iterable[Message],
    identities: Mapping[str, tuple[Identity, ...]],
    files: Mapping[str, FileOutcome],
    options: RenderOptions,
) -> RenderResult:
    """Every slice of a job held in memory, reconciled against the in-scope messages handed in."""
    zone: tzinfo = options.zone()
    convs = {c.id: c for c in conversations}
    if len(convs) != len(conversations):
        raise RenderInputError("duplicate conversation")
    all_msgs: dict[str, dict[str, Message]] = defaultdict(dict)
    slices: dict[tuple[str, date], list[Message]] = defaultdict(list)
    for m in messages:
        if m.conversation_id not in convs:
            raise RenderInputError(f"{m.subject}: unknown conversation")
        if m.ts in all_msgs[m.conversation_id]:
            raise RenderInputError(f"{m.subject} given twice")
        all_msgs[m.conversation_id][m.ts] = m
        if m.in_scope:
            slices[(m.conversation_id, slice_day(m.sent_at, zone))].append(m)

    reconciler = Reconciler(options)
    out: list[RenderedFile] = []
    for conv_id, day in sorted(slices):
        msgs = slices[(conv_id, day)]
        own = {m.ts for m in msgs}
        referenced = {r for m in msgs if (r := _root_of(m)) is not None and r not in own}
        known = all_msgs[conv_id]
        inp = SliceInput(
            job=job,
            conversation=convs[conv_id],
            day=day,
            messages=tuple(msgs),
            roots={r: known[r] for r in sorted(referenced) if r in known},
            missing_roots=frozenset(r for r in referenced if r not in known),
            identities=identities,
            files=files,
        )
        rendered = render_slice(inp, options)
        reconciler.add_slice(inp, rendered)
        out.extend(rendered)
    in_scope = [m.subject for ms in slices.values() for m in ms]
    summary = reconciler.finish(len(in_scope), subject_digest(in_scope))
    return RenderResult(tuple(out), summary)
