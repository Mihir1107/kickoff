"""Job, unit and reconciliation statuses as the chain records them (the product's
`edisc_core.schemas` re-exports them)."""

from __future__ import annotations

from enum import StrEnum


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
