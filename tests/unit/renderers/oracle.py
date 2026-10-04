"""Renderer inputs built straight from the dummy oracle (`Dataset`), independently of the normalizer
and the loader. Expected values in tests come from here, never from rendered output.

A "job" is one collection at `epoch` over the days `[day_from, day_to)` with a thread-parent policy:
- in scope: every message the source shows at `epoch` in the range;
- out of scope: the thread context the policy adds (ADR 0011), as `in_scope=False`;
- versions: each distinct state a message had from its birth epoch to `epoch` (edits, tombstones);
- reactions: the latest snapshot (an empty one when reactions were removed);
- files: a deterministic share is unavailable (reason cycles), the rest stream from `file_bytes`.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from edisc_connector_dummy.dataset import Dataset, Msg, h64
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import ThreadParentPolicy
from edisc_core.canonical import canonical_hash
from edisc_core.idempotency import idempotency_key
from edisc_core.time import day_bounds, ensure_utc
from edisc_renderers.rsmf import (
    ConversationInfo,
    FileAttachment,
    FileOutcome,
    FileUnavailable,
    Identity,
    ItemRef,
    JobInfo,
    Message,
    MessageState,
    Reactions,
)

TENANT = uuid.UUID("00000000-0000-7000-8000-00000000feed")
SOURCE = "slack"
UNAVAILABLE_REASONS = ("deleted", "external", "expired_url", "permission")
_KIND = {
    "channel": "public_channel",
    "private_channel": "private_channel",
    "dm": "im",
    "group_dm": "mpim",
}


def _ref(source_item_id: str, version: int, fingerprint: dict[str, object]) -> ItemRef:
    content = canonical_hash(fingerprint)
    return ItemRef(
        source_item_id, version, idempotency_key(TENANT, SOURCE, source_item_id, content), content
    )


@dataclass(frozen=True)
class OracleJob:
    dataset: Dataset
    job: JobInfo
    conversations: tuple[ConversationInfo, ...]
    messages: tuple[Message, ...]
    identities: dict[str, tuple[Identity, ...]]
    files: dict[str, FileOutcome]

    @property
    def in_scope(self) -> tuple[Message, ...]:
        return tuple(m for m in self.messages if m.in_scope)

    def opener(self, f: FileAttachment) -> Iterator[bytes]:
        data = self.dataset.file_bytes(f.handle)
        for i in range(0, len(data), 1000):
            yield data[i : i + 1000]


def build_job(
    spec: DatasetSpec,
    *,
    epoch: int = 0,
    day_from: int = 0,
    day_to: int | None = None,
    policy: ThreadParentPolicy = ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD,
    unavailable_every: int = 4,
    completeness_basis: str = "source",
) -> OracleJob:
    ds = Dataset(spec)
    ws = spec.workspace_id
    day_to = ds.n_days(epoch) if day_to is None else day_to
    date_from = day_bounds(ds.day(day_from))[0]
    date_to = day_bounds(ds.day(day_to))[0]

    conversations = []
    for i, c in enumerate(ds.conversations()):
        conversations.append(
            ConversationInfo(
                id=c.id,
                slack_type=_KIND[c.kind],  # type: ignore[arg-type]
                workspace_id=ws,
                name=c.name or None,
                members=c.members,
                custodian=c.members[0] if i == 0 else None,
                is_shared=i == 0,
                is_ext_shared=i == 0,
            )
        )

    chosen: dict[tuple[str, str], bool] = {}  # (conversation, ts) -> in scope
    for c in ds.conversations():
        for d in range(day_from, day_to):
            for m in ds.visible_messages(c.id, d, epoch):
                chosen[(c.id, m.ts)] = True
            for _thread, msgs in ds.thread_context(c.id, d, epoch, date_from, date_to, policy):
                for m in msgs:
                    in_range = date_from <= m.sent_at < date_to
                    chosen.setdefault((c.id, m.ts), in_range)

    messages: list[Message] = []
    files: dict[str, FileOutcome] = {}
    for (conv, ts), in_scope in sorted(chosen.items()):
        message = _message(ds, ws, conv, ts, epoch, in_scope)
        messages.append(message)
        for fid in message.current.file_ids:
            if fid not in files:
                files[fid] = _file(ds, ws, fid, unavailable_every)

    return OracleJob(
        dataset=ds,
        job=JobInfo(
            job_id=uuid.UUID(int=h64(spec.seed, "job", epoch, day_from, day_to) << 64 | 0x7000),
            connector_version="dummy/1",
            normalizer_version="0.1.0",
            completeness_basis=completeness_basis,  # type: ignore[arg-type]
        ),
        conversations=tuple(conversations),
        messages=tuple(messages),
        identities=_identities(ds, epoch),
        files=files,
    )


def _find(ds: Dataset, conv: str, ts: str, epoch: int) -> Msg | None:
    for m in ds.visible_messages(conv, ds.day_index(_sent(ts).date()), epoch):
        if m.ts == ts:
            return m
    return None


def _sent(ts: str) -> datetime:
    from edisc_core.time import from_epoch

    return ensure_utc(from_epoch(ts))


def _state_fp(m: Msg) -> dict[str, object]:
    deleted = m.deleted_ts is not None
    return {
        "fp": "oracle.message/1",
        "author": m.user,
        "text": "" if deleted else m.text,
        "subtype": "message_deleted" if deleted else m.subtype,
        "thread_root": m.thread_ts if m.thread_ts and m.thread_ts != m.ts else None,
        "deleted": deleted,
        "files": sorted(f.id for f in m.files),
    }


def _message(ds: Dataset, ws: str, conv: str, ts: str, epoch: int, in_scope: bool) -> Message:
    sid = f"{ws}/{conv}/{ts}"
    states: list[MessageState] = []
    versions: dict[str, int] = {}
    reaction_state: list[list[object]] | None = None
    reaction_versions: dict[str, int] = {}
    reactions: Reactions | None = None
    for ep in range(epoch + 1):
        m = _find(ds, conv, ts, ep)
        if m is None:
            continue
        fp = _state_fp(m)
        content = canonical_hash(fp)
        if not states or states[-1].item.content_hash != content:
            version = versions.setdefault(content, len(versions) + 1)
            states.append(
                MessageState(
                    item=_ref(sid, version, fp),
                    author=m.user,
                    text=str(fp["text"]),
                    subtype=fp["subtype"],  # type: ignore[arg-type]
                    thread_root=fp["thread_root"],  # type: ignore[arg-type]
                    deleted=bool(fp["deleted"]),
                    file_ids=tuple(sorted(f.id for f in m.files)),
                    edited_ts=m.edited[1] if m.edited else None,
                    deleted_ts=m.deleted_ts,
                )
            )
        if m.deleted_ts is None and (m.reactions or reaction_state is not None):
            state = sorted([[name, sorted(users)] for name, users in m.reactions])
            if state != reaction_state:
                reaction_state = state
                rfp = {"fp": "oracle.reactions/1", "message": sid, "reactions": state}
                rcontent = canonical_hash(rfp)
                version = reaction_versions.setdefault(rcontent, len(reaction_versions) + 1)
                reactions = Reactions(
                    item=_ref(f"{sid}#reactions", version, rfp),
                    reactions=tuple((n, tuple(u)) for n, u in m.reactions),
                )
    if not states:
        raise AssertionError(f"{sid} never visible up to epoch {epoch}")
    return Message(
        conversation_id=conv,
        ts=ts,
        sent_at=_sent(ts),
        in_scope=in_scope,
        states=tuple(states),
        reactions=reactions,
    )


def _file(ds: Dataset, ws: str, fid: str, unavailable_every: int) -> FileOutcome:
    name = _file_name(ds, fid)
    pick = h64(ds.seed, "unavailable", fid)
    if unavailable_every and pick % unavailable_every == 0:
        reason = UNAVAILABLE_REASONS[pick // unavailable_every % len(UNAVAILABLE_REASONS)]
        fp: dict[str, object] = {"fp": "oracle.availability/1", "file_id": fid, "reason": reason}
        return FileUnavailable(fid, name, reason, _ref(f"{ws}/file/{fid}#availability", 1, fp))
    data = ds.file_bytes(fid)
    sha = hashlib.sha256(data).hexdigest()
    fp = {"fp": "oracle.file/1", "sha256": sha, "name": name}
    return FileAttachment(fid, name, len(data), sha, _ref(f"{ws}/file/{fid}", 1, fp), handle=fid)


def _file_name(ds: Dataset, fid: str) -> str:
    for conv in ds.conversations():
        for index in range(max(3, ds.spec.messages_per_unit // 8)):
            ref = ds._file(conv.id, index)
            if ref.id == fid:
                return ref.name
    raise KeyError(fid)


def _identities(ds: Dataset, epoch: int) -> dict[str, tuple[Identity, ...]]:
    out: dict[str, tuple[Identity, ...]] = {}
    rename_day = max(1, ds.spec.days // 2)
    for i, u in enumerate(ds.users(epoch)):
        base = Identity(
            user_id=u.id,
            display_name=u.display_name,
            real_name=u.real_name,
            email=u.email,
            team_id=u.team_id,
            is_bot=u.is_bot,
            is_app_user=u.is_app_user,
            deactivated=u.deleted,
        )
        if i == 0:  # renamed mid-dataset: the slice decides which name is in force
            first = Identity(**{**base.__dict__, "display_name": "Alice"})
            later = Identity(
                **{
                    **base.__dict__,
                    "display_name": "Alice (renamed)",
                    "effective_from": day_bounds(ds.day(rename_day))[0],
                }
            )
            out[u.id] = (first, later)
        else:
            out[u.id] = (base,)
    return out
