"""The render oracle: what a case's render must contain, computed from the dummy dataset alone (never
from collected or rendered data), and the coverage features each case exercises.

Per primary message (in scope of the rendered job): its RSMF event type, deletion, number of edits
(the distinct versions the tenant's jobs collected, up to the rendered job, minus one), reaction
names, and attachments (bytes held at any collection up to the job, else a placeholder). Per reply:
where its parent must point, or why it is not rendered. Plus the slices (conversation, local day)
and the context roots.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from edisc_connector_dummy.connector import DummyConnector
from edisc_connector_dummy.dataset import UNINTERPRETABLE, Dataset, Msg, ts_to_datetime
from edisc_connectors_base.types import ThreadParentPolicy
from edisc_renderers.rsmf import load_zone

from .cases import Case

JOIN_LEAVE = {"channel_join": "join", "channel_leave": "leave"}
MESSAGE_SUBTYPES = {None, "bot_message", "me_message", "thread_broadcast", "message_deleted"}


@dataclass
class Expected:
    """One primary event."""

    subject: str  # source item id
    ts: str
    conversation_id: str
    day: date  # local day of its own timestamp (its slice)
    type: str
    deleted: bool
    edits: int
    reactions: frozenset[str]
    files: frozenset[str]  # file ids rendered with their bytes
    unavailable: frozenset[str]  # file ids rendered as placeholders
    root: str | None  # thread root ts for replies
    root_linked: bool  # the job holds the root (in scope, or fetched by the thread policy)
    root_primary_same_slice: bool
    subtype: str | None
    tags: tuple[str, ...]


@dataclass
class RenderOracle:
    primaries: dict[str, Expected] = field(default_factory=dict)
    slices: set[tuple[str, date]] = field(default_factory=set)
    context_roots: set[str] = field(default_factory=set)  # root subjects expected as context events
    features: set[str] = field(default_factory=set)


def _visible(ds: Dataset, case: Case, conv: str, d: int, epoch: int) -> tuple[Msg, ...]:
    msgs = ds.unit_messages(conv, d, epoch)
    if case.source == "export" or ds.omits_deleted:  # exports hold no deleted messages
        return tuple(m for m in msgs if m.deleted_ts is None)
    return msgs


def _state(m: Msg) -> tuple[object, ...]:
    """The normalizer's version fingerprint, as the oracle sees it (ADR 0004)."""
    deleted = m.deleted_ts is not None
    return (
        "" if deleted else m.text,
        deleted,
        () if deleted else tuple(sorted(f.id for f in m.files)),
        m.user,
        "message_deleted" if deleted else m.subtype,
        m.thread_ts if m.thread_ts and m.thread_ts != m.ts else None,
    )


def _range(ds: Dataset, case: Case, epoch: int) -> tuple[datetime, datetime]:
    start = datetime.combine(ds.day(case.first_day), datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(ds.day(ds.n_days(epoch)), datetime.min.time(), tzinfo=UTC)
    return start, end


def _scoped_conversations(ds: Dataset, case: Case) -> list[str]:
    if case.export_tier == "public_only" and case.source == "export":
        return [c.id for c in ds.conversations() if c.kind == "channel"]
    if case.custodians:
        members = ds.conversations()[0].members[: case.custodians]
        return [c.id for c in ds.conversations() if set(c.members) & set(members)]
    return [c.id for c in ds.conversations()]


def build(case: Case) -> RenderOracle:
    ds = Dataset(case.spec)
    epoch = case.epochs[-1]
    zone = load_zone(case.options.time_zone)
    start, end = _range(ds, case, epoch)
    out = RenderOracle()
    ws = case.spec.workspace_id
    convs = _scoped_conversations(ds, case)

    for conv in convs:
        in_scope: dict[str, Msg] = {}
        all_days: dict[str, Msg] = {}
        for d in range(ds.n_days(epoch)):
            for m in _visible(ds, case, conv, d, epoch):
                all_days[m.ts] = m
                if start <= m.sent_at < end:
                    in_scope[m.ts] = m
        # roots the job holds: in scope, or fetched by the thread policy for a reply in scope
        linked_roots = set(in_scope)
        if case.policy is not ThreadParentPolicy.REPLIES_ONLY:
            linked_roots |= {
                m.thread_ts for m in in_scope.values()
                if m.thread_ts and m.thread_ts != m.ts and m.thread_ts in all_days
            }  # fmt: skip
        for ts, m in in_scope.items():
            day = m.sent_at.astimezone(zone).date()
            out.slices.add((conv, day))
            root = m.thread_ts if m.thread_ts and m.thread_ts != m.ts else None
            root_msg = in_scope.get(root) if root else None
            versions = []
            # reactions: the latest snapshot collected, which a deletion does not replace (a
            # tombstone carries no reactions, and no new snapshot is recorded for it)
            reactions: frozenset[str] = frozenset()
            for e in case.epochs:
                if e == epoch:
                    seen = m
                else:  # earlier jobs collected the whole dataset
                    d0 = ds.day_index(m.sent_at.date())
                    seen = next((x for x in _visible(ds, case, conv, d0, e) if x.ts == ts), None)
                if seen is not None and (not versions or versions[-1] != _state(seen)):
                    versions.append(_state(seen))
                if seen is not None and seen.deleted_ts is None:
                    reactions = frozenset(n for n, _ in seen.reactions)
            held, unavailable = set(), set()
            for f in m.files if m.deleted_ts is None else ():
                if case.source == "export" or any(
                    DummyConnector.file_unavailable_reason(ds, e, f.id) is None for e in case.epochs
                ):
                    held.add(f.id)
                else:
                    unavailable.add(f.id)
            subtype = "message_deleted" if m.deleted_ts is not None else m.subtype
            etype = JOIN_LEAVE.get(
                subtype or "", "message" if subtype in MESSAGE_SUBTYPES else "unknown"
            )
            out.primaries[f"{ws}/{conv}/{ts}"] = Expected(
                subject=f"{ws}/{conv}/{ts}", ts=ts, conversation_id=conv, day=day, type=etype,
                deleted=m.deleted_ts is not None, edits=len(versions) - 1,
                reactions=reactions,
                files=frozenset(held), unavailable=frozenset(unavailable), root=root,
                root_linked=bool(root and root in linked_roots),
                root_primary_same_slice=bool(
                    root_msg is not None and root_msg.sent_at.astimezone(zone).date() == day
                ),
                subtype=subtype, tags=m.tags,
            )  # fmt: skip
            if (
                root
                and case.options.include_context
                and root in linked_roots
                and not (root_msg is not None and root_msg.sent_at.astimezone(zone).date() == day)
            ):
                out.context_roots.add(f"{ws}/{conv}/{root}")
    out.features = features(case, ds, out)
    return out


def features(case: Case, ds: Dataset, o: RenderOracle) -> set[str]:
    """What the case exercises (the coverage matrix is checked over every case)."""
    f = {f"dialect:{case.source}:{case.spec.dialect}"}
    if case.source == "export":
        f.add(f"export_tier:{case.export_tier}")
    f.add(f"zone:{case.options.time_zone}")
    f.add(f"context:{'on' if case.options.include_context else 'off'}")
    f.add(f"policy:{case.policy.value}")
    if case.custodians > 1:
        f.add("custodians:several")
    kinds = {ds.conversation(c).kind for c, _ in o.slices}
    f |= {f"conversation:{k}" for k in kinds}
    for e in o.primaries.values():
        f.add(f"type:{e.type}")
        if (
            e.subtype in ("thread_broadcast", "me_message", "bot_message")
            or e.subtype in UNINTERPRETABLE
        ):
            f.add(f"subtype:{e.subtype}")
        if e.deleted:
            f.add("deleted")
        if e.edits:
            f.add("edits")
        if "hint_only_edit" in e.tags:
            f.add("hint_only_edit")
        if e.reactions:
            f.add("reactions")
        if e.files:
            f.add("file:held")
        if e.unavailable:
            f.add("file:unavailable")
        if e.root and e.root_primary_same_slice:
            f.add("thread:root_same_slice")
        if e.root and not e.root_linked:
            f.add("thread:root_not_collected")
        if e.root and e.root_linked and not e.root_primary_same_slice:
            f.add("thread:root_elsewhere")
        for t in ("emoji", "rtl", "zero_width", "combining", "long", "attachment_only"):
            if t in e.tags:
                f.add(f"text:{t}")
        if e.subtype == "thread_broadcast" or "thread_broadcast" in e.tags:
            f.add("broadcast")
            if e.root and not e.root_primary_same_slice:
                f.add("broadcast:parent_outside_slice")
            if e.edits:
                f.add("broadcast:edited")
            if e.deleted:
                f.add("broadcast:deleted")
    days = {d for _, d in o.slices}
    zone = load_zone(case.options.time_zone)
    for d in days:
        start = datetime.combine(d, datetime.min.time(), tzinfo=zone)
        length = (start + timedelta(days=1)).astimezone(UTC) - start.astimezone(UTC)
        if length != timedelta(days=1):
            f.add(f"day_length:{int(length.total_seconds() // 3600)}h")
    offset = (
        datetime.combine(min(days), datetime.min.time(), tzinfo=zone).utcoffset() if days else None
    )
    if offset is not None and offset.total_seconds() % 3600:
        f.add(f"offset:{int(offset.total_seconds() // 60) % 60}min")
    if case.expect_files is not None:
        f.add(f"files:{case.expect_files}@batch{case.batch_size}")
    if case.spec.messages_per_unit >= 10_000:
        f.add(f"slice_events:{case.spec.messages_per_unit}")
    if 1 in case.epochs:
        f.add("renamed:channel_and_user")
    if 2 in case.epochs:
        f.add("archived:channel")
    return f


def iter_events(manifest: dict[str, object]) -> Iterator[dict[str, object]]:
    yield from manifest["events"]  # type: ignore[misc]


def ts_day(ts: str, zone_name: str) -> date:
    return ts_to_datetime(ts).astimezone(load_zone(zone_name)).date()


COVERAGE = {
    "dialect:live:slack", "dialect:live:slack_history", "dialect:export:slack_history",
    "export_tier:full", "export_tier:public_only",
    "zone:UTC", "zone:America/New_York", "zone:Asia/Kolkata", "zone:Asia/Kathmandu",
    "day_length:23h", "day_length:25h", "offset:30min", "offset:45min",
    "context:on", "context:off", "policy:replies_only", "policy:include_parent_and_thread",
    "custodians:several",
    "conversation:channel", "conversation:private_channel", "conversation:dm", "conversation:group_dm",
    "type:message", "type:join", "type:leave", "type:unknown",
    "subtype:thread_broadcast", "subtype:me_message", "subtype:bot_message",
    "subtype:channel_topic", "subtype:pinned_item",
    "deleted", "edits", "hint_only_edit", "reactions", "file:held", "file:unavailable",
    "thread:root_same_slice", "thread:root_elsewhere", "thread:root_not_collected",
    "text:emoji", "text:rtl", "text:zero_width", "text:combining", "text:long", "text:attachment_only",
    "broadcast", "broadcast:parent_outside_slice", "broadcast:edited", "broadcast:deleted",
    "files:3@batch3", "files:4@batch3", "slice_events:10000", "slice_events:10001",
    "renamed:channel_and_user", "archived:channel",
}  # fmt: skip
