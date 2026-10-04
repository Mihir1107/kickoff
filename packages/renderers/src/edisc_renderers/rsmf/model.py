"""Renderer inputs (ADR 0015 §1). Plain data only: the worker loader builds it from items and
derivations (M15 step 3); tests build it from the dummy oracle. Nothing here touches a database,
object storage or a clock."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Literal

from edisc_core.time import ensure_utc
from edisc_renderers.rsmf.runtime import UnknownZoneError, load_zone

RENDER_CAP = 10_000
"""At most this many events per file, context events included (ADR 0015 §2)."""

SlackConversationType = Literal["im", "mpim", "public_channel", "private_channel"]
CompletenessBasis = Literal["source", "archive"]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RenderError(Exception):
    """Base class: every renderer failure is loud. A failed render produces no file."""


class RenderInputError(RenderError):
    """The inputs are inconsistent (a loader bug or corrupted data), so nothing is rendered."""


class ManifestInvalidError(RenderError):
    """A manifest failed the vendored schema or the structural checks."""


class ReconciliationError(RenderError):
    """The rendered events do not account for the job's in-scope items exactly once."""


class EvidenceMismatchError(RenderError):
    """File bytes streamed into the zip do not match the recorded size or SHA-256."""


class ZipLimitError(RenderError):
    """The zip would need ZIP64 (over 4 GiB or 65,535 entries). Not supported in M15."""


@dataclass(frozen=True)
class ItemRef:
    """A collected item (one version): what custody and `X-RSMF-SourceHash` know it by."""

    source_item_id: str
    version: int
    idempotency_key: str
    content_hash: str

    def __post_init__(self) -> None:
        if not (_HEX64.match(self.idempotency_key) and _HEX64.match(self.content_hash)):
            raise RenderInputError(f"{self.source_item_id}: key and content hash must be hex64")
        if self.version < 1:
            raise RenderInputError(f"{self.source_item_id}: version must be >= 1")


@dataclass(frozen=True)
class MessageState:
    """One collected version of a message (its latest derivation)."""

    item: ItemRef
    author: str  # Slack user id, or bot id when there is no user
    text: str  # exactly as collected
    subtype: str | None
    thread_root: str | None  # root ts for replies, None for roots and unthreaded messages
    deleted: bool
    file_ids: tuple[str, ...] = ()
    edited_ts: str | None = None  # Slack `edited.ts` hint recorded with this version
    deleted_ts: str | None = None


@dataclass(frozen=True)
class Reactions:
    """The latest reaction snapshot of a message: `(name, user ids)` pairs."""

    item: ItemRef
    reactions: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class Message:
    """One message subject: every collected version in order (the last is current)."""

    conversation_id: str
    ts: str
    sent_at: datetime
    in_scope: bool
    states: tuple[MessageState, ...]
    reactions: Reactions | None = None

    def __post_init__(self) -> None:
        if not self.states:
            raise RenderInputError(f"{self.conversation_id}/{self.ts}: a message needs a state")
        ensure_utc(self.sent_at)
        subjects = {s.item.source_item_id for s in self.states}
        if len(subjects) != 1:
            raise RenderInputError(f"{self.conversation_id}/{self.ts}: states of several subjects")

    @property
    def current(self) -> MessageState:
        return self.states[-1]

    @property
    def subject(self) -> str:
        return self.states[-1].item.source_item_id


@dataclass(frozen=True)
class FileAttachment:
    """Collected file bytes, pinned. `handle` is opaque to the renderer (the opener resolves it)."""

    file_id: str
    name: str
    size: int
    sha256: str
    item: ItemRef
    handle: str

    def __post_init__(self) -> None:
        if not _FILE_ID.match(self.file_id):
            raise RenderInputError(f"unsafe file id {self.file_id!r}")
        if self.size < 0 or not _HEX64.match(self.sha256):
            raise RenderInputError(f"file {self.file_id}: bad size or sha256")


@dataclass(frozen=True)
class FileUnavailable:
    """The source refused the file. `item` is the `file_unavailable` event item."""

    file_id: str
    name: str
    reason: str
    item: ItemRef

    def __post_init__(self) -> None:
        if not _FILE_ID.match(self.file_id):
            raise RenderInputError(f"unsafe file id {self.file_id!r}")


FileOutcome = FileAttachment | FileUnavailable
FileOpener = Callable[[FileAttachment], Iterable[bytes]]
"""Streams a file's pinned bytes in chunks. The renderer checks size and SHA-256 as it goes."""
AsyncFileOpener = Callable[[FileAttachment], AsyncIterator[bytes]]
"""The same, async: the worker loader reads pinned evidence from object storage."""


@dataclass(frozen=True)
class Identity:
    """An identity snapshot. `effective_from=None` means in force from the start."""

    user_id: str
    effective_from: datetime | None = None
    display_name: str | None = None
    real_name: str | None = None
    email: str | None = None
    team_id: str | None = None
    is_bot: bool | None = None
    is_app_user: bool | None = None
    deactivated: bool | None = None


@dataclass(frozen=True)
class ConversationInfo:
    id: str
    slack_type: SlackConversationType | None  # None: the source's metadata was not collected
    workspace_id: str
    name: str | None = None
    members: tuple[str, ...] | None = None  # known membership; else the observed participants
    custodians: tuple[str, ...] = ()  # user ids of every custodian scope covering the conversation
    is_shared: bool | None = None
    is_ext_shared: bool | None = None
    archived: bool | None = None
    topic: str | None = None
    purpose: str | None = None
    known_names: tuple[str, ...] = ()  # every name the conversation had (renames stay searchable)

    def __post_init__(self) -> None:
        if list(self.custodians) != sorted(set(self.custodians)):
            raise RenderInputError(f"{self.id}: custodians must be unique and sorted")
        if list(self.known_names) != sorted(set(self.known_names)):
            raise RenderInputError(f"{self.id}: known names must be unique and sorted")


@dataclass(frozen=True)
class JobInfo:
    job_id: uuid.UUID
    connector_version: str
    normalizer_version: str
    completeness_basis: CompletenessBasis


@dataclass(frozen=True)
class RenderOptions:
    """Recorded in the render's custody stream (ADR 0015 §7)."""

    include_context: bool = True
    time_zone: str = "UTC"
    cap: int = RENDER_CAP

    def __post_init__(self) -> None:
        if not 2 <= self.cap <= RENDER_CAP:  # a reply plus its context root must fit in a file
            raise RenderInputError(f"cap must be within 2..{RENDER_CAP}")
        self.zone()

    def zone(self) -> tzinfo:
        """From the pinned tzdata package only, never the system zoneinfo (runtime.load_zone)."""
        try:
            return load_zone(self.time_zone)
        except UnknownZoneError as exc:
            raise RenderInputError(str(exc)) from exc

    def as_payload(self) -> dict[str, object]:
        return {
            "include_context": self.include_context,
            "time_zone": self.time_zone,
            "cap": self.cap,
        }


@dataclass(frozen=True)
class SliceInput:
    """Everything one slice (one conversation, one local day) needs.

    - `messages`: the job's IN-SCOPE messages whose own timestamp is inside the slice.
    - `roots`: thread roots referenced by those replies and not among them (any scope, any day).
    - `missing_roots`: root ts the loader looked for and the job does not hold. A referenced root
      that is in neither `messages`, `roots` nor `missing_roots` is a loader bug and fails the render.
    - `files`: the outcome of every file the rendered states reference.
    """

    job: JobInfo
    conversation: ConversationInfo
    day: date
    messages: tuple[Message, ...]
    roots: Mapping[str, Message] = field(default_factory=dict)
    missing_roots: frozenset[str] = frozenset()
    identities: Mapping[str, tuple[Identity, ...]] = field(default_factory=dict)
    files: Mapping[str, FileOutcome] = field(default_factory=dict)
