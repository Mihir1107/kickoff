"""Workflow/activity contracts: small, serializable values only (ADR 0012 section 1).

Ids, unit keys, counters and flags. Never raw data, tokens or item lists. Sandbox-safe: no imports
beyond the standard library, because workflows import this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from edisc_core.settings import Settings


def task_queue(source: str) -> str:
    return f"collect-{source}"


MAINTENANCE_QUEUE = "maintenance"
EXPORTS_QUEUE = "exports"  # Slack export hash-lock-validate (ADR 0014)
RENDERS_QUEUE = "renders"  # prefix: one queue per renderer/Unicode/tzdata triple (ADR 0015 §15)


def export_workflow_id(export_id: str) -> str:
    return f"export-{export_id}"


def render_workflow_id(render_id: str) -> str:
    return f"render-{render_id}"


def render_task_queue(renderer_version: str, unicode_version: str, tzdata_version: str) -> str:
    """The queue of the workers that render exactly these versions (ADR 0015 §15): a render runs on
    the queue of the versions it recorded at creation, so it never reaches a worker whose output
    bytes would differ."""
    return f"{RENDERS_QUEUE}.r{renderer_version}.u{unicode_version}.tz{tzdata_version}"


def unit_workflow_id(job_id: str, unit_key: str) -> str:
    return f"{job_id}/{unit_key}"


class ErrorClass(StrEnum):
    UNIT_INTEGRITY = (
        "UnitIntegrity"  # the unit fails; other units go on (completed_with_failed_units)
    )
    JOB_INTEGRITY = "JobIntegrity"  # the whole job fails at the next batch boundary
    AUTH_REQUIRED = (
        "AuthRequired"  # every running job on the connection pauses for re-authorization
    )
    JOB_CLOSED = "JobClosed"  # the job was sealed/terminal under us: stop, nothing to record
    RENDER_INTEGRITY = "RenderIntegrity"  # a render's inputs or outputs disagree: the render fails
    TRANSIENT = "Transient"  # retried with backoff; when exhausted the unit goes to retry_later
    UNCLASSIFIED = "Unclassified"  # retried a few times, then the unit fails with the type recorded


NON_RETRYABLE = frozenset(
    {
        ErrorClass.UNIT_INTEGRITY,
        ErrorClass.JOB_INTEGRITY,
        ErrorClass.AUTH_REQUIRED,
        ErrorClass.JOB_CLOSED,
        ErrorClass.RENDER_INTEGRITY,
    }
)


class UnitOutcome(StrEnum):
    DONE = "done"  # collected and reconciled
    FAILED = "failed"  # unit-scoped failure recorded
    RETRY_LATER = (
        "retry_later"  # transient budget exhausted; the parent re-schedules after the cool-down
    )
    STOPPED = (
        "stopped"  # cancel / job failure / re-auth pause / closed job: stopped at a batch boundary
    )
    JOB_FAILED = "job_failed"  # this unit hit a job-scoped integrity failure
    PAUSED = "paused"  # this unit hit an auth failure; the connection's jobs are paused


@dataclass(frozen=True)
class RunConfig:
    """Tuning carried in workflow input (workflows cannot read settings or the environment)."""

    max_units_in_flight: int = 8
    pages_per_activity: int = 50
    unit_iterations_per_run: int = 200
    job_poll_seconds: float = 120
    start_to_close_seconds: float = 1800
    heartbeat_timeout_seconds: float = 60
    control_timeout_seconds: float = 300
    retry_initial_seconds: float = 1
    retry_max_seconds: float = 60
    max_attempts: int = 25
    job_iterations_per_run: int = 500

    @classmethod
    def from_settings(cls, settings: Settings) -> RunConfig:
        return cls(
            max_units_in_flight=settings.max_units_in_flight,
            pages_per_activity=settings.unit_pages_per_activity,
            unit_iterations_per_run=settings.unit_iterations_per_run,
            job_poll_seconds=settings.job_poll_seconds,
            start_to_close_seconds=settings.activity_start_to_close_seconds,
            heartbeat_timeout_seconds=settings.activity_heartbeat_timeout_seconds,
            retry_initial_seconds=settings.activity_retry_initial_seconds,
            retry_max_seconds=settings.activity_retry_max_seconds,
            max_attempts=settings.activity_max_attempts,
        )


@dataclass(frozen=True)
class ExportRef:
    """One uploaded export to hash, lock and validate. Timeouts come from settings via the API."""

    tenant_id: str
    export_id: str
    heartbeat_timeout_seconds: float = 60
    retry_initial_seconds: float = 1
    retry_max_seconds: float = 60
    max_attempts: int = 25


@dataclass(frozen=True)
class RenderRef:
    """One render (ADR 0015 §14). The render id is the workflow's whole identity: a retry or a re-run
    after a crash renders the same id, so it reproduces (and dedups against) the same stored bytes."""

    tenant_id: str
    render_id: str
    heartbeat_timeout_seconds: float = 60
    retry_initial_seconds: float = 1
    retry_max_seconds: float = 60
    max_attempts: int = 25
    control_timeout_seconds: float = 300
    render_timeout_seconds: float = 43_200

    @classmethod
    def from_settings(cls, tenant_id: str, render_id: str, settings: Settings) -> RenderRef:
        return cls(
            tenant_id=tenant_id,
            render_id=render_id,
            heartbeat_timeout_seconds=settings.activity_heartbeat_timeout_seconds,
            retry_initial_seconds=settings.activity_retry_initial_seconds,
            retry_max_seconds=settings.activity_retry_max_seconds,
            max_attempts=settings.activity_max_attempts,
            render_timeout_seconds=settings.render_start_to_close_seconds,
        )


@dataclass(frozen=True)
class RenderFailure:
    render: RenderRef
    error_type: str
    error: str


@dataclass(frozen=True)
class JobRef:
    tenant_id: str
    job_id: str


@dataclass(frozen=True)
class UnitRef:
    tenant_id: str
    job_id: str
    unit_key: str


@dataclass(frozen=True)
class CollectRequest:
    unit: UnitRef
    max_pages: int


@dataclass(frozen=True)
class UnitFailure:
    unit: UnitRef
    error_type: str
    error: str


@dataclass(frozen=True)
class StopRequest:
    job: JobRef
    reason: str  # cancel | job_failure
    detail: str


@dataclass(frozen=True)
class PauseRequest:
    job: JobRef
    reason: str


@dataclass(frozen=True)
class OverviewRequest:
    job: JobRef
    limit: int


@dataclass(frozen=True)
class JobOverview:
    status: str
    stop_reason: str | None
    sealed: bool
    ready: list[str]  # startable now (the parent skips those already in flight)
    remaining: list[str]  # not yet done or failed
    next_retry_in: float | None  # seconds until the earliest retry_later unit is due


@dataclass(frozen=True)
class ChildrenRequest:
    job: JobRef
    unit_keys: list[str]


@dataclass(frozen=True)
class JobInput:
    tenant_id: str
    job_id: str
    config: RunConfig = field(default_factory=RunConfig)
    # carried across continue-as-new
    in_flight: list[str] = field(default_factory=list)
    enumerated: bool = False
    cancel_requested: bool = False
    stop_sent: bool = False


@dataclass(frozen=True)
class UnitInput:
    tenant_id: str
    job_id: str
    unit_key: str
    config: RunConfig = field(default_factory=RunConfig)
    iterations: int = 0  # collect_pages calls so far (carried across continue-as-new)


@dataclass(frozen=True)
class UnitFinished:
    unit_key: str
    outcome: str
