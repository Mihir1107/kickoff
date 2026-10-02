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
    FILE_UNAVAILABLE = "file_unavailable"  # the source refused the bytes (reason recorded)
    FILE_BECAME_AVAILABLE = "file_became_available"
    ACCESS_LOST = "access_lost"  # a whole conversation became inaccessible
    ACCESS_RESTORED = "access_restored"


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
    COMPLETED_WITH_FAILED_UNITS = "completed_with_failed_units"  # never clean (ADR 0012 R4)
    # every unit matched against an uploaded export: complete relative to the EXPORT only (ADR 0014)
    COMPLETED_AGAINST_ARCHIVE = "completed_against_archive"
    PAUSED_AWAITING_REAUTH = "paused_awaiting_reauth"  # not terminal
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_clean(self) -> bool:
        """Only ``completed`` is a clean completion (ADR 0005)."""
        return self is JobStatus.COMPLETED

    @property
    def is_terminal(self) -> bool:
        return self not in (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED_AWAITING_REAUTH)


class UnitStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    RETRY_LATER = "retry_later"  # transient budget exhausted: cool-down, re-scheduled by the parent
    PAUSED = "paused"


class ReconStatus(StrEnum):
    PENDING = "pending"
    MATCHED = "matched"
    GAP = "gap"
    SURPLUS = "surplus"
    UNVERIFIABLE = "unverifiable"
    FAILED = "failed"
    ACCESS_LOST = (
        "access_lost"  # the conversation became inaccessible: a gap, never per-message absence
    )
    NOT_APPLICABLE = "not_applicable"  # e.g. the directory unit
    # every element of the unit's export day file accounted for; says nothing about the workspace
    MATCHED_AGAINST_ARCHIVE = "matched_against_archive"


# ADR 0014 section 4: shown verbatim wherever an archive-relative status is (API and report)
ARCHIVE_CAVEAT = (
    "Completeness was verified against the provided Slack export only. Every entry of the export was "
    "accounted for, but the export's own completeness relative to the Slack workspace was NOT verified: "
    "content excluded by the plan, the export's date range, Slack retention settings or the export "
    "settings cannot be detected from the export."
)


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
