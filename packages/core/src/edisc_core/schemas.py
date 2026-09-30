"""Canonical, source-independent domain schemas (Pydantic v2).

The canonical message is what renderers and search consume. It never carries raw bytes; it points at
evidence via ``raw_storage_key`` + ``raw_json_path``. All timestamps are aware UTC (``UtcDatetime``).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from edisc_core.time import UtcDatetime


class ConversationType(StrEnum):
    CHANNEL = "channel"
    PRIVATE_CHANNEL = "private_channel"
    DM = "dm"
    GROUP_DM = "group_dm"


class MessageType(StrEnum):
    MESSAGE = "message"
    SYSTEM = "system"
    BOT = "bot"


class ItemType(StrEnum):
    MESSAGE = "message"
    FILE = "file"
    EVENT = "event"


class EventKind(StrEnum):
    """Subtype of ``ItemType.EVENT`` items (ADR 0004)."""

    REACTION_SNAPSHOT = "reaction_snapshot"
    IDENTITY_SNAPSHOT = "identity_snapshot"
    CHANGE_OBSERVATION = "change_observation"
    NO_LONGER_OBSERVED = "no_longer_observed"  # absence is never deletion
    OBSERVED_AGAIN = "observed_again"


class ScopeType(StrEnum):
    CUSTODIAN = "custodian"
    CHANNEL = "channel"
    CHAT = "chat"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    COMPLETED_UNVERIFIED = "completed_unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_clean(self) -> bool:
        """Only ``completed`` is a clean completion (ADR 0005)."""
        return self is JobStatus.COMPLETED

    @property
    def is_terminal(self) -> bool:
        return self not in (JobStatus.PENDING, JobStatus.RUNNING)


class UnitStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ReconStatus(StrEnum):
    PENDING = "pending"
    MATCHED = "matched"
    GAP = "gap"
    SURPLUS = "surplus"
    UNVERIFIABLE = "unverifiable"
    FAILED = "failed"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Mention(_Frozen):
    identity_external_id: str
    display_text: str | None = None


class Reaction(_Frozen):
    name: str
    user_external_ids: tuple[str, ...]


class CanonicalMessage(_Frozen):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    job_id: uuid.UUID
    source: str
    source_workspace_id: str
    conversation_id: str
    conversation_type: ConversationType
    source_message_id: str
    thread_root_id: str | None = None
    parent_message_id: str | None = None
    author_identity_id: uuid.UUID | None = None
    author_external_id: str
    custodian_id: uuid.UUID | None = None
    sent_at_utc: UtcDatetime
    edited_at_utc: UtcDatetime | None = None
    deleted_at_utc: UtcDatetime | None = None
    message_type: MessageType
    body_text: str
    body_raw: str | None = Field(default=None, description="Rich body as delivered (blocks/HTML).")
    mentions: tuple[Mention, ...] = ()
    reactions: tuple[Reaction, ...] = ()
    attachment_item_ids: tuple[uuid.UUID, ...] = ()
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_storage_key: str
    raw_json_path: str
    connector_version: str
    collected_at_utc: UtcDatetime
