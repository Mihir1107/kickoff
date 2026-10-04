"""The dummy dataset: a pure, seed-derived model of a workspace. It is also the ORACLE.

Expected counts and expected item sets come from here (computed from the seed), never from what a
pipeline collected. Same ``(spec, epoch)`` => identical model => byte-identical raw pages.

Structure at epoch 0: ``conversations x days x messages_per_unit`` messages, exactly. Each unit has:
slot 0 at 00:00:00.000000 UTC, the last slot at 23:59:59.999000 UTC (slice boundaries), a system
message, threads (same-day replies and replies on the NEXT day), files (some shared between messages),
reactions, edits, bot and app messages, and messy content (emoji, RTL, zero-width and combining
characters, a very long message, an attachment with an empty body).

Epochs: epoch ``k >= 1`` adds one new day of messages and changes existing ones (edits, edit markers
that change without a content change, deletions, reaction changes, renamed users). Edits made in an
epoch are timestamped after the original collection window.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import ThreadParentPolicy

DAY_US = 86_400_000_000
LAST_US = 86_399_999_000  # 23:59:59.999000

# Non-ASCII content is written as escapes so no editor can normalize it (ADR 0004: text is hashed as-is).
FLAVORS: dict[str, str] = {
    "emoji": "Deploy done \U0001f680\U0001f389 family \U0001f469‍\U0001f469‍\U0001f467‍\U0001f466 flag \U0001f1ee\U0001f1f3 ok \U0001f44d\U0001f3fd",
    "rtl": "שלום עולם — مرحبا بالعالم (ticket 123, ok?)",
    "zero_width": "zero​width‌non‍joiner and ﻿BOM inside",
    "combining": "café vs café, Z͑ͫ̓àlgo, ñ and ñ",
}
FLAVOR_ORDER = ("emoji", "rtl", "zero_width", "combining", "long", "attachment_only")
WORDS = (
    "budget",
    "deploy",
    "contract",
    "draft",
    "review",
    "invoice",
    "meeting",
    "privileged",
    "ship",
    "delay",
    "audit",
    "merger",
)
REACTIONS = ("thumbsup", "eyes", "white_check_mark", "tada", "joy", "pray")
MIMES = (
    ("pdf", "application/pdf"),
    ("png", "image/png"),
    ("txt", "text/plain"),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
)


def h64(seed: int, *parts: object) -> int:
    digest = hashlib.sha256(("|".join([str(seed), *map(str, parts)])).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def unit(seed: int, *parts: object) -> float:
    """Deterministic float in [0, 1)."""
    return h64(seed, *parts) / 2**64


@dataclass(frozen=True)
class Conversation:
    id: str
    kind: str  # channel | private_channel | dm | group_dm
    name: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class User:
    id: str
    team_id: str
    name: str
    real_name: str
    display_name: str
    email: str | None
    avatar_hash: str
    is_bot: bool = False
    is_app_user: bool = False
    deleted: bool = False
    is_stranger: bool = False  # external member of a shared channel
    bot_id: str | None = None


@dataclass(frozen=True)
class FileRef:
    id: str
    name: str
    mimetype: str
    filetype: str
    size: int


@dataclass(frozen=True)
class Msg:
    conversation_id: str
    day_index: int
    slot: int
    ts: str  # Slack-style "seconds.micros", unique per conversation
    user: str
    text: str
    subtype: str | None
    thread_ts: str | None  # parent ts for replies, own ts for parents, else None
    is_parent: bool
    reply_ts: tuple[str, ...]  # for parents: replies that exist at this epoch
    edited: tuple[str, str] | None  # (user, ts)
    deleted_ts: str | None
    reactions: tuple[tuple[str, tuple[str, ...]], ...]
    files: tuple[FileRef, ...]
    born_epoch: int
    tags: tuple[str, ...]  # what this message is here to exercise (for tests)

    @property
    def sent_at(self) -> datetime:
        return ts_to_datetime(self.ts)


def ts_to_datetime(ts: str) -> datetime:
    seconds, micros = ts.split(".")
    return datetime.fromtimestamp(int(seconds), UTC) + timedelta(microseconds=int(micros))


def ts_of(day: date, offset_us: int) -> str:
    base = calendar.timegm(day.timetuple())
    return f"{base + offset_us // 1_000_000}.{offset_us % 1_000_000:06d}"


@dataclass(frozen=True)
class _Slot:
    role: str  # top | system | reply_same | reply_prev
    parent_slot: int | None  # slot of the parent (same day or previous day)
    is_parent: bool


class Dataset:
    def __init__(self, spec: DatasetSpec) -> None:
        self.spec = spec
        self.seed = spec.seed
        # per-instance caches (lru_cache on methods would pin instances globally)
        self._slots = lru_cache(maxsize=256)(self._slots_uncached)
        self._unit = lru_cache(maxsize=64)(self._unit_uncached)

    # ------------------------------------------------------------------ structure
    def n_days(self, epoch: int) -> int:
        return self.spec.days + epoch

    def day(self, index: int) -> date:
        return self.spec.start_day + timedelta(days=index)

    def day_index(self, day: date) -> int:
        return (day - self.spec.start_day).days

    def epoch_time(self, epoch: int) -> str:
        """When epoch ``k``'s changes happened: after the last day that existed at that epoch."""
        return ts_of(self.day(self.n_days(epoch)), 3_600_000_000 * 9)

    def users(self, epoch: int) -> tuple[User, ...]:
        team = self.spec.workspace_id
        out: list[User] = []
        for i in range(self.spec.users):
            base = f"user{i:02d}"
            display = base.capitalize()
            if i == 5 and epoch >= 1:
                display = "User05 (renamed at epoch 1)"  # the directory changes between collections
            out.append(
                User(
                    id=f"U{i:05d}DUMMY",
                    team_id="T0EXTERNAL" if i == 2 else team,  # 2: shared-channel guest
                    name=base,
                    real_name=f"{base.capitalize()} Example",
                    display_name=display,
                    email=None if i in (3, 4) else f"{base}@example.test",
                    avatar_hash=f"{h64(self.seed, 'avatar', i, display):016x}"[:12],
                    is_bot=i in (3, 4),  # 3: classic bot, 4: app user
                    is_app_user=i == 4,
                    deleted=i == 1,  # deactivated, but authored messages
                    is_stranger=i == 2,
                    bot_id=f"B{i:05d}DUMMY" if i in (3, 4) else None,
                )
            )
        return tuple(out)

    def display_name_at(self, user_index: int, day_index: int, epoch: int) -> str:
        """User 0 is renamed MID-DATASET: messages from the second half carry the new display name."""
        if user_index == 0:
            return "Alice" if day_index < max(1, self.spec.days // 2) else "Alice (renamed)"
        return self.users(epoch)[user_index].display_name

    def conversations(self) -> tuple[Conversation, ...]:
        kinds = ("channel", "private_channel", "dm", "group_dm")
        prefix = {"channel": "C", "private_channel": "G", "dm": "D", "group_dm": "G"}
        users = [u.id for u in self.users(0)]
        out = []
        for c in range(self.spec.conversations):
            kind = kinds[c % 4]
            size = {"dm": 2, "group_dm": 4}.get(kind, max(6, len(users) // 2))
            if c == 0:  # conversation 0 contains every special identity
                members = users[: max(size, 7)]
            else:
                start = h64(self.seed, "members", c) % len(users)
                members = [users[(start + k) % len(users)] for k in range(size)]
            name = f"mpdm-{c}" if kind == "group_dm" else (f"conv-{c}" if kind != "dm" else "")
            out.append(Conversation(f"{prefix[kind]}{c:05d}DUMMY", kind, name, tuple(members)))
        return tuple(out)

    def conversation_state(self, conversation_id: str, epoch: int) -> dict[str, object]:
        """What ``conversations.list`` (+ members) shows at ``epoch``. Conversation 0 is renamed at
        epoch 1 and conversation 1 is archived from epoch 2, so the metadata has versions."""
        conv = self.conversation(conversation_id)
        index = [c.id for c in self.conversations()].index(conversation_id)
        name = conv.name
        if index == 0 and epoch >= 1 and name:
            name = f"{name}-renamed"
        return {
            "kind": conv.kind,
            "name": name,
            "topic": f"Topic of {name}" if conv.kind in ("channel", "private_channel") else "",
            "purpose": f"Purpose of conversation {index}"
            if conv.kind in ("channel", "private_channel")
            else "",
            "members": list(conv.members),
            "archived": index == 1 and epoch >= 2,
            "shared": index == 0,
        }

    def conversation(self, conversation_id: str) -> Conversation:
        for conv in self.conversations():
            if conv.id == conversation_id:
                return conv
        raise KeyError(conversation_id)

    # ------------------------------------------------------------------ slots (structure of one unit)
    def _offset_us(self, conv: str, d: int, slot: int) -> int:
        k = self.spec.messages_per_unit
        if slot == 0:
            return 0
        if slot == k - 1:
            return LAST_US
        step = LAST_US // (k - 1)
        jitter = int((unit(self.seed, "jit", conv, d, slot) - 0.5) * step * 0.8)
        return slot * step + jitter

    def ts(self, conv: str, d: int, slot: int) -> str:
        return ts_of(self.day(d), self._offset_us(conv, d, slot))

    def _slots_uncached(self, conv: str, d: int) -> tuple[_Slot, ...]:
        spec, k = self.spec, self.spec.messages_per_unit
        prev_parents = (
            [i for i, s in enumerate(self._slots(conv, d - 1)) if s.is_parent] if d > 0 else []
        )
        roles: list[tuple[str, int | None]] = []
        parents_so_far: list[int] = []
        parent_flags: list[bool] = []
        for i in range(k):
            role: tuple[str, int | None]
            if i in (0, k - 1):
                role = ("top", None)
            elif i == 1:
                role = ("system", None)
            elif i == 2:
                role = ("top", None)  # forced thread parent (see flags below)
            elif i == 3:
                role = ("reply_same", 2)  # forced same-day reply
            elif i == 4 and prev_parents:
                role = (
                    "reply_prev",
                    prev_parents[0],
                )  # forced reply to a parent of the PREVIOUS day
            else:
                r = unit(self.seed, "role", conv, d, i)
                if prev_parents and r < spec.p_reply_prev_day:
                    role = (
                        "reply_prev",
                        prev_parents[h64(self.seed, "pp", conv, d, i) % len(prev_parents)],
                    )
                elif parents_so_far and r < spec.p_reply_prev_day + spec.p_reply_same_day:
                    role = (
                        "reply_same",
                        parents_so_far[h64(self.seed, "sp", conv, d, i) % len(parents_so_far)],
                    )
                else:
                    role = ("top", None)
            is_parent = (
                role[0] == "top"
                and i not in (0, k - 1)
                and (i == 2 or unit(self.seed, "parent", conv, d, i) < spec.p_thread_parent)
            )
            if is_parent:
                parents_so_far.append(i)
            roles.append(role)
            parent_flags.append(is_parent)
        return tuple(_Slot(r, p, f) for (r, p), f in zip(roles, parent_flags, strict=True))

    # ------------------------------------------------------------------ messages (true state at an epoch)
    def unit_messages(self, conversation_id: str, day_index: int, epoch: int) -> tuple[Msg, ...]:
        """Every message of the conversation-day as it truly is at ``epoch``, ordered by ts."""
        if not 0 <= day_index < self.n_days(epoch):
            return ()
        return self._unit(conversation_id, day_index, epoch)

    @property
    def omits_deleted(self) -> bool:
        return self.spec.dialect == "slack_history"

    def visible_messages(self, conversation_id: str, day_index: int, epoch: int) -> tuple[Msg, ...]:
        """What the SOURCE shows: everything, except deleted messages in the omission dialect."""
        msgs = self.unit_messages(conversation_id, day_index, epoch)
        return tuple(m for m in msgs if m.deleted_ts is None) if self.omits_deleted else msgs

    def expected_count(self, conversation_id: str, day_index: int, epoch: int) -> int:
        return len(self.visible_messages(conversation_id, day_index, epoch))

    def expected_ids(self, conversation_id: str, day_index: int, epoch: int) -> frozenset[str]:
        return frozenset(m.ts for m in self.visible_messages(conversation_id, day_index, epoch))

    def total_messages(self, epoch: int) -> int:
        return self.spec.conversations * self.n_days(epoch) * self.spec.messages_per_unit

    def _unit_uncached(self, conv: str, d: int, epoch: int) -> tuple[Msg, ...]:
        spec, k = self.spec, self.spec.messages_per_unit
        slots = self._slots(conv, d)
        born = max(0, d - (spec.days - 1))  # days added by epoch k are born at epoch k
        conversation = self.conversation(conv)
        members = conversation.members
        user_ids = [u.id for u in self.users(epoch)]
        # replies that exist at this epoch, per parent slot of THIS day
        replies: dict[int, list[str]] = {i: [] for i, s in enumerate(slots) if s.is_parent}
        for i, s in enumerate(slots):
            if s.role == "reply_same" and s.parent_slot is not None:
                replies[s.parent_slot].append(self.ts(conv, d, i))
        if d + 1 < self.n_days(epoch):
            for i, s in enumerate(self._slots(conv, d + 1)):
                if s.role == "reply_prev" and s.parent_slot is not None:
                    replies[s.parent_slot].append(self.ts(conv, d + 1, i))

        out: list[Msg] = []
        for i, s in enumerate(slots):
            ts = self.ts(conv, d, i)
            tags: list[str] = []
            if i == 0:
                tags.append("boundary_start")
            if i == k - 1:
                tags.append("boundary_end")
            # author: conversation 0 / day 0 forces the special identities onto fixed slots
            if conv == self.conversations()[0].id and d == 0 and 5 <= i <= 9:
                author = user_ids[i - 5]
                tags.append("special_identity")
            else:
                author = members[h64(self.seed, "author", conv, d, i) % len(members)]
            author_index = user_ids.index(author)
            author_user = self.users(epoch)[author_index]

            flavor = "plain"
            if d == 0 and 5 <= i <= 10:
                flavor = FLAVOR_ORDER[i - 5]
            else:
                pick = h64(self.seed, "flavor", conv, d, i) % 24
                if pick < len(FLAVOR_ORDER):
                    flavor = FLAVOR_ORDER[pick]
            subtype: str | None = None
            files: tuple[FileRef, ...] = ()
            if s.role == "system":
                subtype, text, flavor = (
                    "channel_join",
                    f"<@{author}> has joined the channel",
                    "system",
                )
            else:
                words = " ".join(
                    WORDS[h64(self.seed, "w", conv, d, i, n) % len(WORDS)] for n in range(6)
                )
                plain = f"[{conv} d{d} #{i}] {words}"
                if flavor == "long":
                    text = (plain + " ") * (40_000 // (len(plain) + 1) + 1)
                elif flavor == "attachment_only":
                    text = ""
                elif flavor in FLAVORS:
                    text = f"{FLAVORS[flavor]} [{conv} d{d} #{i}]"
                else:
                    text = plain
                if flavor == "attachment_only" or unit(self.seed, "file", conv, d, i) < spec.p_file:
                    files = (
                        self._file(conv, h64(self.seed, "fileidx", conv, d, i) % max(3, k // 8)),
                    )
                if author_user.is_bot:
                    subtype = "bot_message"
            if flavor != "plain":
                tags.append(flavor)

            thread_ts: str | None = None
            if s.is_parent:
                thread_ts = ts
                tags.append("thread_parent")
            elif s.role == "reply_same" and s.parent_slot is not None:
                thread_ts = self.ts(conv, d, s.parent_slot)
                tags.append("reply_same_day")
            elif s.role == "reply_prev" and s.parent_slot is not None:
                thread_ts = self.ts(conv, d - 1, s.parent_slot)
                tags.append("reply_next_day")

            # ---- state: epoch 0 baseline, then one step per epoch
            edited: tuple[str, str] | None = None
            deleted_ts: str | None = None
            reactions = (
                self._reactions(conv, ts, 0, members)
                if unit(self.seed, "r0", conv, ts) < 0.25
                else ()
            )
            if s.role != "system" and unit(self.seed, "e0", conv, ts) < 0.05:
                edited = (
                    author,
                    ts_of(self.day(d), min(LAST_US, self._offset_us(conv, d, i) + 60_000_000)),
                )
                tags.append("edited_before_collection")
            for step in range(born + 1, epoch + 1):
                if deleted_ts is not None or s.role == "system":
                    break
                # conversation 0 / day 0 / epoch 1 forces one of each change onto fixed slots (5..8)
                forced = conv == self.conversations()[0].id and d == 0 and step == 1 and 5 <= i <= 8
                roll = unit(self.seed, "evo", conv, ts, step)
                when = self.epoch_time(step)
                if forced:
                    change = {5: "edit", 6: "hint", 7: "delete", 8: None}[i]
                elif roll < spec.p_delete:
                    change = "delete"
                elif roll < spec.p_delete + spec.p_edit:
                    change = "edit"
                elif roll < spec.p_delete + spec.p_edit + spec.p_hint_only_edit:
                    change = "hint"
                else:
                    change = None
                if change == "delete":
                    deleted_ts = when
                    tags.append("deleted")
                elif change == "edit":
                    text = f"[edited at epoch {step}] {text}"
                    edited = (author, when)
                    tags.append("edited")
                elif change == "hint":
                    edited = (author, when)  # the edit marker moves, the content does not
                    tags.append("hint_only_edit")
                if (forced and i == 8) or (
                    not forced and unit(self.seed, "rc", conv, ts, step) < spec.p_reaction_change
                ):
                    reactions = self._reactions(conv, ts, step, members)
                    tags.append("reaction_change")

            out.append(
                Msg(
                    conversation_id=conv,
                    day_index=d,
                    slot=i,
                    ts=ts,
                    user=author,
                    text=text,
                    subtype=subtype,
                    thread_ts=thread_ts,
                    is_parent=s.is_parent,
                    reply_ts=tuple(sorted(replies.get(i, ()))),
                    edited=edited,
                    deleted_ts=deleted_ts,
                    reactions=reactions,
                    files=files if deleted_ts is None else (),
                    born_epoch=born,
                    tags=tuple(tags),
                )
            )
        return tuple(sorted(out, key=lambda m: (int(m.ts.split(".")[0]), int(m.ts.split(".")[1]))))

    def _reactions(
        self, conv: str, ts: str, step: int, members: tuple[str, ...]
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        count = 1 + h64(self.seed, "rn", conv, ts, step) % 3
        out: list[tuple[str, tuple[str, ...]]] = []
        for n in range(count):
            name = REACTIONS[h64(self.seed, "rname", conv, ts, step, n) % len(REACTIONS)]
            who = sorted(
                {
                    members[h64(self.seed, "rw", conv, ts, step, n, j) % len(members)]
                    for j in range(1 + n)
                }
            )
            if name not in [o[0] for o in out]:
                out.append((name, tuple(who)))
        return tuple(out)

    # ------------------------------------------------------------------ files
    def _file(self, conv: str, index: int) -> FileRef:
        ext, mime = MIMES[h64(self.seed, "mime", conv, index) % len(MIMES)]
        file_id = f"F{h64(self.seed, 'fid', conv, index):016X}"[:12]
        return FileRef(
            file_id,
            f"attachment-{index}.{ext}",
            mime,
            ext,
            200 + h64(self.seed, "fsize", conv, index) % 3800,
        )

    def file_bytes(self, file_id: str) -> bytes:
        size = None
        for conv in self.conversations():
            for index in range(max(3, self.spec.messages_per_unit // 8)):
                ref = self._file(conv.id, index)
                if ref.id == file_id:
                    size = ref.size
                    break
            if size is not None:
                break
        if size is None:
            raise KeyError(file_id)
        out = bytearray()
        counter = 0
        while len(out) < size:
            out += hashlib.sha256(f"{self.seed}|bytes|{file_id}|{counter}".encode()).digest()
            counter += 1
        return bytes(out[:size])

    # ------------------------------------------------------------------ threads and the parent policy
    def thread(self, conversation_id: str, thread_ts: str, epoch: int) -> tuple[Msg, ...]:
        """Parent first, then every reply that exists at ``epoch`` (same day and next day), by ts."""
        d = self.day_index(ts_to_datetime(thread_ts).date())
        msgs = [
            m for m in self.visible_messages(conversation_id, d, epoch) if m.thread_ts == thread_ts
        ]
        msgs += [
            m
            for m in self.visible_messages(conversation_id, d + 1, epoch)
            if m.thread_ts == thread_ts
        ]
        parent = [m for m in msgs if m.ts == thread_ts]
        rest = sorted((m for m in msgs if m.ts != thread_ts), key=lambda m: m.ts)
        return (*parent, *rest)

    def out_of_range_parents(
        self, conversation_id: str, day_index: int, epoch: int, date_from: datetime
    ) -> tuple[str, ...]:
        """thread_ts of threads with a reply in this unit whose parent was sent before ``date_from``."""
        seen: list[str] = []
        for m in self.visible_messages(conversation_id, day_index, epoch):
            if (
                m.thread_ts
                and m.thread_ts != m.ts
                and ts_to_datetime(m.thread_ts) < date_from
                and m.thread_ts not in seen
            ):
                seen.append(m.thread_ts)
        return tuple(seen)

    def after_range_threads(
        self, conversation_id: str, day_index: int, epoch: int, date_to: datetime
    ) -> tuple[str, ...]:
        """Mirror case: parents IN this unit with replies sent at/after ``date_to``."""
        return tuple(
            m.ts
            for m in self.visible_messages(conversation_id, day_index, epoch)
            if m.is_parent and any(ts_to_datetime(r) >= date_to for r in m.reply_ts)
        )

    def thread_context(
        self,
        conversation_id: str,
        day_index: int,
        epoch: int,
        date_from: datetime,
        date_to: datetime,
        policy: ThreadParentPolicy,
    ) -> tuple[tuple[str, tuple[Msg, ...]], ...]:
        """What the policy adds for this unit, as (thread_ts, messages):
        - replies in range whose parent is before the range: the parent (parent_only) or the full
          thread (include_parent_and_thread);
        - mirror case (include_parent_and_thread only): parents in range with replies after the range.
        """
        if policy is ThreadParentPolicy.REPLIES_ONLY:
            return ()
        out: list[tuple[str, tuple[Msg, ...]]] = []
        for thread_ts in self.out_of_range_parents(conversation_id, day_index, epoch, date_from):
            full = self.thread(conversation_id, thread_ts, epoch)
            out.append(
                (thread_ts, full[:1] if policy is ThreadParentPolicy.INCLUDE_PARENT_ONLY else full)
            )
        if policy is ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD:
            out.extend(
                (thread_ts, self.thread(conversation_id, thread_ts, epoch))
                for thread_ts in self.after_range_threads(
                    conversation_id, day_index, epoch, date_to
                )
            )
        return tuple(out)
