"""Dataset specification for the dummy source. Everything the generator produces is a pure function of
(spec, epoch): same spec + epoch => byte-identical output."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CountMode(StrEnum):
    TRUE = "true"  # the source reports true counts (even while dropping items: the gap must be detected)
    UNAVAILABLE = "unavailable"  # the source cannot count (the job must end completed_unverified)


class FailureSpec(BaseModel):
    """Deterministic failure injection, keyed by ``seed`` and the request. Rates are per request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int = 0
    exception_rate: float = Field(default=0.0, ge=0, le=1)
    timeout_rate: float = Field(default=0.0, ge=0, le=1)
    throttle_rate: float = Field(default=0.0, ge=0, le=1)  # source answers 429 with Retry-After
    drop_rate: float = Field(default=0.0, ge=0, le=1)  # items silently missing from pages
    max_consecutive: int = Field(default=2, ge=1)  # a failing request fails this many times at most
    retry_after_seconds: float = Field(default=0.05, gt=0)
    # files the source will not hand over (reason cycles deleted/external/expired_url/permission;
    # expired_url is transient: the file downloads fine from the next epoch on)
    file_unavailable_rate: float = Field(default=0.0, ge=0, le=1)
    # conversation index -> first epoch at which the whole conversation is inaccessible
    inaccessible_from_epoch: dict[int, int] = Field(default_factory=dict)
    # conversation indexes whose cursor is corrupt from the second page on (unit-scoped integrity)
    corrupt_conversations: tuple[int, ...] = ()
    # conversation indexes whose every request fails with an upstream error (transient that never heals)
    unavailable_conversations: tuple[int, ...] = ()


class DatasetSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    # "slack": Discovery-style, deleted messages come back as tombstones.
    # "slack_history": standard conversations.history, deleted messages are OMITTED entirely.
    # A "teams" dialect can be added without touching the model.
    dialect: Literal["slack", "slack_history"] = "slack"
    workspace_id: str = "T0DUMMY01"
    conversations: int = Field(default=4, ge=1)
    days: int = Field(default=3, ge=1)  # base days at epoch 0; every epoch adds one more day
    messages_per_unit: int = Field(default=40, ge=12)  # exactly this many per conversation-day
    start_day: date = date(2026, 1, 5)
    users: int = Field(default=12, ge=8)
    page_size: int = Field(default=15, ge=2)
    messy_pagination: bool = True  # empty pages, overlaps, out-of-order items
    count_mode: CountMode = CountMode.TRUE
    failures: FailureSpec = FailureSpec()

    # structure probabilities (deterministic per slot via the seed)
    p_thread_parent: float = 0.15
    p_reply_same_day: float = 0.20
    p_reply_prev_day: float = 0.06
    p_file: float = 0.10
    # file sizes: min + a deterministic share of span. The defaults keep every earlier spec's bytes;
    # larger sizes put files over a render's native threshold (ADR 0015 §20)
    file_size_min: int = Field(default=200, ge=0)
    file_size_span: int = Field(default=3800, ge=1)
    # message kinds (0.3.0); conversation 0 also forces one of each (dataset docstring)
    p_broadcast: float = 0.10  # a reply also sent to the channel (subtype thread_broadcast)
    p_me_message: float = 0.03
    p_uninterpretable: float = 0.02  # subtypes the renderer cannot interpret (rendered `unknown`)
    # evolution per epoch
    p_edit: float = 0.06
    p_hint_only_edit: float = 0.03
    p_delete: float = 0.03
    p_reaction_change: float = 0.10
