"""Connector-facing types. Connectors are thin: authenticate, enumerate, fetch raw bytes. No interpretation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from edisc_core.schemas import ScopeType
from edisc_core.time import ensure_utc


class ThreadParentPolicy(StrEnum):
    """What to collect for thread replies that are INSIDE the date range while their parent is OUTSIDE.

    PROPOSED default, pending product-owner confirmation (docs/adr/0011): include the parent and the
    full thread, with everything outside the range marked out-of-range (context, not responsive scope).
    """

    INCLUDE_PARENT_AND_THREAD = "include_parent_and_thread"
    INCLUDE_PARENT_ONLY = "include_parent_only"
    REPLIES_ONLY = "replies_only"


DEFAULT_THREAD_PARENT_POLICY = ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD


class BatchKind(StrEnum):
    HISTORY = "history"  # messages of the unit's conversation-day
    THREAD_CONTEXT = "thread_context"  # a thread fetched because of the thread-parent policy
    DIRECTORY = "directory"  # users / identities


@dataclass(frozen=True)
class Connection:
    """What a connector needs to talk to one connected workspace. Secrets are NOT here: connectors get
    tokens from the token store inside activities (ADR 0009)."""

    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    source: str
    workspace_id: str
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectionInfo:
    plan_tier: str
    granted_scopes: tuple[str, ...]
    blind_spots: tuple[str, ...]  # stated in every collection report
    can_report_counts: bool


@dataclass(frozen=True)
class CollectionScope:
    scope_type: ScopeType
    external_id: str
    date_from: datetime  # inclusive
    date_to: datetime  # exclusive
    thread_parent_policy: ThreadParentPolicy = DEFAULT_THREAD_PARENT_POLICY

    def __post_init__(self) -> None:
        if ensure_utc(self.date_from) >= ensure_utc(self.date_to):
            raise ValueError("scope date_from must be before date_to")


@dataclass(frozen=True)
class WorkUnit:
    """One conversation x one UTC day (ADR 0005)."""

    conversation_id: str
    day: date

    @property
    def unit_key(self) -> str:
        return f"{self.conversation_id}/{self.day.isoformat()}"


Cursor = str
"""Opaque, connector-defined. Re-fetching from a cursor must return the same remaining batches."""


@dataclass(frozen=True)
class RawBatch:
    body: bytes  # the exact bytes the source returned for one request: stored as-is in WORM
    next_cursor: Cursor | None  # None when the unit is exhausted
    kind: BatchKind
    request: Mapping[str, str]  # method + parameters, for provenance (never secrets)


class FileUnavailableReason(StrEnum):
    DELETED = "deleted"
    EXTERNAL_OR_HIDDEN = "external_or_hidden"
    EXPIRED_URL = "expired_url"
    PERMISSION = "permission"


class AccessLossReason(StrEnum):
    NOT_IN_CHANNEL = "not_in_channel"
    CHANNEL_NOT_FOUND = "channel_not_found"
    ARCHIVED = "archived"
    ACCESS_REVOKED = "access_revoked"


class FileUnavailableError(Exception):
    """The source refused a file download for a known reason. Not a crash: recorded as a gap."""

    def __init__(self, file_ref: str, reason: FileUnavailableReason, response: bytes) -> None:
        super().__init__(f"file {file_ref} unavailable: {reason.value}")
        self.file_ref, self.reason, self.response = file_ref, reason, response


class ConversationInaccessibleError(Exception):
    """The whole conversation is no longer accessible (one observation, never a per-message flood)."""

    def __init__(self, conversation_id: str, reason: AccessLossReason, response: bytes) -> None:
        super().__init__(f"conversation {conversation_id} inaccessible: {reason.value}")
        self.conversation_id, self.reason, self.response = conversation_id, reason, response
