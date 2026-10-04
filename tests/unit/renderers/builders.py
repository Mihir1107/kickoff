"""Small hand-built renderer inputs for behaviour tests."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import date

from edisc_core.time import from_epoch, parse_utc
from edisc_renderers.rsmf import (
    ConversationInfo,
    FileAttachment,
    FileUnavailable,
    Identity,
    ItemRef,
    JobInfo,
    Message,
    MessageState,
    Reactions,
    RenderedFile,
    RenderOptions,
    SliceInput,
    render_slice,
)
from tests.unit.renderers.emlcheck import Parsed, check_eml

JOB = JobInfo(uuid.UUID("01900000-0000-7000-8000-000000000001"), "slack/1", "0.1.0", "source")
WS = "T0TEST"


def h(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def ref(sid: str, version: int = 1) -> ItemRef:
    return ItemRef(sid, version, h("key", sid, version), h("content", sid, version))


def ts_at(iso: str) -> str:
    dt = parse_utc(iso)
    return f"{int(dt.timestamp())}.{dt.microsecond:06d}"


def conv(slack_type: str = "public_channel", **kw: object) -> ConversationInfo:
    args: dict[str, object] = {
        "id": "C1",
        "slack_type": slack_type,
        "workspace_id": WS,
        "name": "general",
        "members": ("U1", "U2"),
    }
    args.update(kw)
    return ConversationInfo(**args)  # type: ignore[arg-type]


def msg(
    iso: str,
    *,
    author: str = "U1",
    text: str = "hello",
    subtype: str | None = None,
    root: str | None = None,
    in_scope: bool = True,
    deleted: bool = False,
    files: tuple[str, ...] = (),
    edits: tuple[tuple[str, str | None], ...] = (),  # earlier (text, edited_ts) versions
    reactions: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
    conversation: str = "C1",
    deleted_ts: str | None = None,
) -> Message:
    ts = ts_at(iso)
    sid = f"{WS}/{conversation}/{ts}"
    states = [
        MessageState(ref(sid, i + 1), author, t, subtype, root, False, files, edited_ts=e)
        for i, (t, e) in enumerate(edits)
    ]
    states.append(
        MessageState(
            ref(sid, len(states) + 1),
            author,
            "" if deleted else text,
            "message_deleted" if deleted else subtype,
            root,
            deleted,
            () if deleted else files,
            edited_ts=None,
            deleted_ts=deleted_ts,
        )
    )
    return Message(
        conversation_id=conversation,
        ts=ts,
        sent_at=from_epoch(ts),
        in_scope=in_scope,
        states=tuple(states),
        reactions=Reactions(ref(f"{sid}#reactions"), reactions) if reactions is not None else None,
    )


def attachment(fid: str, name: str, data: bytes) -> FileAttachment:
    return FileAttachment(
        fid, name, len(data), hashlib.sha256(data).hexdigest(), ref(f"{WS}/file/{fid}"), fid
    )


def unavailable(fid: str, name: str, reason: str) -> FileUnavailable:
    return FileUnavailable(fid, name, reason, ref(f"{WS}/file/{fid}#availability"))


def slice_input(
    day: date,
    messages: list[Message],
    *,
    conversation: ConversationInfo | None = None,
    roots: dict[str, Message] | None = None,
    missing: frozenset[str] = frozenset(),
    identities: dict[str, tuple[Identity, ...]] | None = None,
    files: dict[str, FileAttachment | FileUnavailable] | None = None,
    job: JobInfo = JOB,
) -> SliceInput:
    return SliceInput(
        job=job,
        conversation=conversation or conv(),
        day=day,
        messages=tuple(messages),
        roots=roots or {},
        missing_roots=missing,
        identities=identities or {},
        files=files or {},
    )


def opener_for(blobs: dict[str, bytes]):  # type: ignore[no-untyped-def]
    def opener(f: FileAttachment) -> Iterator[bytes]:
        yield blobs[f.handle]

    return opener


def render(
    inp: SliceInput, options: RenderOptions | None = None, blobs: dict[str, bytes] | None = None
) -> list[tuple[RenderedFile, Parsed]]:
    files = render_slice(inp, options or RenderOptions())
    opener = opener_for(blobs or {})
    return [(f, check_eml(b"".join(f.stream(opener)))) for f in files]
