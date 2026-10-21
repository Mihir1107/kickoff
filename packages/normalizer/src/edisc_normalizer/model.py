"""Normalizer data model. Everything here is plain data: the normalizer is a pure function of
(raw bytes, context, prior state, file evidence) -> derived records."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from edisc_core.schemas import EventKind, ItemType
from edisc_verify.canonical import canonical_source_hash
from edisc_verify.idempotency import idempotency_key
from edisc_verify.lone_surrogates import FIELD, has_lone, make_valid

NORMALIZER_VERSION = "0.3.0"
"""Recorded on every derived record (item_derivations). Bump when derivation logic changes. Changing a
FINGERPRINT definition additionally changes content hashes (see the fp tags in slack.py) and is a
deliberate, reviewed change (ADR 0004). 0.3.0: lone surrogates are collected (ADR 0018 §23): source
hashes write them as escapes (``canonical_source_json``), a derived record holds their replacement
text plus ``lone_surrogates`` (JSON Pointer -> exact UTF-16 code units); before, such a page failed
the job. Every other derivation is byte-identical to 0.2.0's."""


@dataclass(frozen=True)
class EvidenceRef:
    """The WORM object a derived record points into."""

    evidence_id: uuid.UUID
    storage_key: str


@dataclass(frozen=True)
class FileEvidence:
    """A downloaded attachment: bytes hashed while streamed into WORM (ADR 0002)."""

    file_id: str
    sha256: str
    size: int
    evidence: EvidenceRef


@dataclass(frozen=True)
class FileUnavailable:
    """The source refused a referenced file (reason as reported). Recorded, never raised."""

    file_id: str
    reason: str


@dataclass(frozen=True)
class NormalizeContext:
    tenant_id: uuid.UUID
    source: str  # connector source, e.g. "dummy", "slack"
    workspace_id: str
    conversation_id: str | None  # None for directory pages
    unit_day: date | None
    date_from: datetime | None  # collection scope (inclusive)
    date_to: datetime | None  # collection scope (exclusive)
    normalizer_version: str = NORMALIZER_VERSION
    # several scopes (ADR 0005 amendment): every [from, to) range that applies to this conversation;
    # when given, an item is in scope if any of them contains it (date_from/date_to are then the envelope)
    ranges: tuple[tuple[datetime, datetime], ...] = ()
    # "api": Web API response pages ({"ok": true, "messages": [...]}); "export": a Slack export file, a
    # bare JSON array (day files of messages, users.json of members). Same fingerprints either way.
    dialect: str = "api"


@dataclass(frozen=True)
class PriorState:
    """What is already recorded for one subject (a source_item_id), loaded by the store."""

    latest_content_hash: str | None = None
    known_content_hashes: frozenset[str] = frozenset()
    current_hints: Mapping[str, str] = field(default_factory=dict)
    change_count: int = 0  # items already in the subject's "#change" stream
    observation_status: str | None = None  # latest "#observation" status
    observation_count: int = 0


EMPTY_PRIOR = PriorState()


@dataclass(frozen=True)
class Derived:
    """One record to persist: an ``items`` row (if new) plus its ``item_derivations`` row."""

    source_item_id: str
    item_type: ItemType
    event_kind: EventKind | None
    fingerprint: Mapping[str, Any]
    raw_hash: str
    evidence: EvidenceRef
    json_path: str
    parent: tuple[str, str] | None  # (parent source_item_id, parent content_hash)
    sent_at: datetime | None
    change_hints: Mapping[str, str]
    in_scope: bool
    derived: Mapping[str, Any]

    def __post_init__(self) -> None:
        """The record as STORED (ADR 0018 §23): valid Unicode everywhere. A string holding a lone
        surrogate becomes its replacement text and ``derived["lone_surrogates"]`` records its exact
        code units by JSON Pointer; hints are stored replaced by the normalizer. The fingerprint is
        hashed losslessly (it is never stored)."""
        if FIELD in self.derived:
            raise ValueError(f"{self.source_item_id}: '{FIELD}' is reserved in a derived record")
        valid, found = make_valid(dict(self.derived))
        if found:
            object.__setattr__(self, "derived", {**valid, FIELD: found})
        if any(has_lone(v) for v in self.change_hints.values()):
            raise ValueError(f"{self.source_item_id}: change hints must be stored replaced")

    @property
    def content_hash(self) -> str:
        return canonical_source_hash(dict(self.fingerprint))

    def idempotency_key(self, tenant_id: uuid.UUID, source: str) -> str:
        return idempotency_key(tenant_id, source, self.source_item_id, self.content_hash)


@dataclass(frozen=True)
class PageResult:
    items: tuple[Derived, ...]
    observed_messages: frozenset[str]  # message source_item_ids of THIS unit seen on the page
    subjects: frozenset[str]  # every subject whose prior state was consulted
    unavailable_files: frozenset[str] = frozenset()  # file ids the source refused: a unit gap


@dataclass(frozen=True)
class FileMeta:
    file_id: str
    name: str
    mimetype: str
    size: int
