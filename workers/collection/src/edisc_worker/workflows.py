"""Temporal workflows (ADR 0012).

``CollectionJobWorkflow`` (id ``{job_id}``) fans out ``CollectUnitWorkflow`` children
(id ``{job_id}/{unit_key}``, ``ABANDON``) with at most N in flight, and finalizes the job. The DATABASE
is the source of truth: the parent never awaits child handles. Children report by signal (fast path).
The parent also re-reads the DB and asks Temporal which in-flight children are closed (backstop), so
correctness never depends on a signal being delivered.

Determinism: no I/O, clock, randomness or environment here; activities are called by name and every
value in history is an id, key, counter or flag. Changes that alter the command sequence go behind
``workflow.patched`` (ADR 0012 section 6; replay tests in tests/unit/temporal).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    FailureError,
    WorkflowAlreadyStartedError,
)
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from edisc_worker.contracts import (
        ChildrenRequest,
        CollectRequest,
        ErrorClass,
        ExportRef,
        JobInput,
        JobOverview,
        JobRef,
        OverviewRequest,
        PauseRequest,
        RunConfig,
        StopRequest,
        UnitFailure,
        UnitFinished,
        UnitInput,
        UnitOutcome,
        UnitRef,
        unit_workflow_id,
    )

TERMINAL = frozenset(
    {
        "completed",
        "completed_with_gaps",
        "completed_unverified",
        "completed_with_failed_units",
        "failed",
        "cancelled",
    }
)


def _retry(cfg: RunConfig, *, unlimited: bool = False) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=cfg.retry_initial_seconds),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=cfg.retry_max_seconds),
        maximum_attempts=0 if unlimited else cfg.max_attempts,
        non_retryable_error_types=[
            ErrorClass.UNIT_INTEGRITY.value,
            ErrorClass.JOB_INTEGRITY.value,
            ErrorClass.AUTH_REQUIRED.value,
            ErrorClass.JOB_CLOSED.value,
        ],
    )


async def _control[T](cfg: RunConfig, name: str, arg: Any, result_type: type[T]) -> T:
    """Short DB-only activity. Retried without limit (transient DB/S3 outages must not orphan a job);
    non-retryable classes still fail at once."""
    result: T = await workflow.execute_activity(
        name,
        arg,
        result_type=result_type,
        start_to_close_timeout=timedelta(seconds=cfg.control_timeout_seconds),
        heartbeat_timeout=timedelta(seconds=cfg.heartbeat_timeout_seconds),
        retry_policy=_retry(cfg, unlimited=True),
    )
    return result


def error_class(err: ActivityError) -> ErrorClass:
    """Class of a failed activity. A timeout (worker died repeatedly) counts as transient."""
    cause = err.cause
    if isinstance(cause, ApplicationError):
        known = {c.value for c in ErrorClass}
        return ErrorClass(cause.type) if cause.type in known else ErrorClass.UNCLASSIFIED
    return ErrorClass.TRANSIENT


def error_text(err: ActivityError) -> tuple[str, str]:
    cause = err.cause
    if isinstance(cause, ApplicationError):
        details = cause.details
        return (str(details[0]) if details else cause.type or "ApplicationError"), cause.message
    return type(cause).__name__ if cause else "ActivityError", str(cause or err)


@workflow.defn(name="CollectUnitWorkflow")
class CollectUnitWorkflow:
    @workflow.run
    async def run(self, inp: UnitInput) -> str:
        cfg = inp.config
        ref = UnitRef(inp.tenant_id, inp.job_id, inp.unit_key)
        iterations = inp.iterations
        outcome: UnitOutcome | None = None
        while outcome is None:
            try:
                result = await workflow.execute_activity(
                    "collect_pages",
                    CollectRequest(ref, cfg.pages_per_activity),
                    result_type=str,
                    start_to_close_timeout=timedelta(seconds=cfg.start_to_close_seconds),
                    heartbeat_timeout=timedelta(seconds=cfg.heartbeat_timeout_seconds),
                    retry_policy=_retry(cfg),
                )
            except ActivityError as err:
                outcome = await self._on_error(cfg, ref, err)
                break
            if result == "done":
                try:
                    await workflow.execute_activity(
                        "finalize_unit",
                        ref,
                        result_type=str,
                        start_to_close_timeout=timedelta(seconds=cfg.start_to_close_seconds),
                        heartbeat_timeout=timedelta(seconds=cfg.heartbeat_timeout_seconds),
                        retry_policy=_retry(cfg),
                    )
                    outcome = UnitOutcome.DONE
                except ActivityError as err:
                    outcome = await self._on_error(cfg, ref, err)
            elif result == "stopped":
                outcome = UnitOutcome.STOPPED
            else:  # "more": resume from the DB checkpoint
                iterations += 1
                if (
                    iterations % cfg.unit_iterations_per_run == 0
                    or workflow.info().is_continue_as_new_suggested()
                ):
                    workflow.continue_as_new(
                        UnitInput(
                            inp.tenant_id, inp.job_id, inp.unit_key, cfg, iterations=iterations
                        )
                    )
        await self._notify_parent(inp, outcome)
        return outcome.value

    @staticmethod
    async def _on_error(cfg: RunConfig, ref: UnitRef, err: ActivityError) -> UnitOutcome:
        cls = error_class(err)
        error_type, message = error_text(err)
        job = JobRef(ref.tenant_id, ref.job_id)
        if cls in (ErrorClass.UNIT_INTEGRITY, ErrorClass.UNCLASSIFIED):
            await _control(cfg, "fail_unit", UnitFailure(ref, error_type, message), type(None))
            return UnitOutcome.FAILED
        if cls is ErrorClass.JOB_INTEGRITY:
            await _control(
                cfg,
                "request_stop",
                StopRequest(job, "job_failure", f"{ref.unit_key}: {error_type}: {message}"),
                bool,
            )
            return UnitOutcome.JOB_FAILED
        if cls is ErrorClass.AUTH_REQUIRED:
            await _control(cfg, "pause_for_reauth", PauseRequest(job, message), int)
            return UnitOutcome.PAUSED
        if cls is ErrorClass.JOB_CLOSED:
            return UnitOutcome.STOPPED
        status = await _control(cfg, "defer_unit", UnitFailure(ref, error_type, message), str)
        return UnitOutcome.FAILED if status == "failed" else UnitOutcome.RETRY_LATER

    @staticmethod
    async def _notify_parent(inp: UnitInput, outcome: UnitOutcome) -> None:
        """Fast path only: the parent's DB/describe backstop covers a closed or missing parent."""
        parent = workflow.get_external_workflow_handle(inp.job_id)
        try:
            await parent.signal("unit_finished", UnitFinished(inp.unit_key, outcome.value))
        except FailureError:
            workflow.logger.info("parent %s not running; outcome is in the DB", inp.job_id)


@workflow.defn(name="CollectionJobWorkflow")
class CollectionJobWorkflow:
    def __init__(self) -> None:
        self._finished: list[str] = []
        self._cancel = False
        self._wake = False

    @workflow.signal(name="unit_finished")
    def unit_finished(self, msg: UnitFinished) -> None:
        self._finished.append(msg.unit_key)
        self._wake = True

    @workflow.signal(name="cancel")
    def cancel(self) -> None:
        self._cancel = True
        self._wake = True

    @workflow.signal(name="wake")
    def wake(self) -> None:
        """Sent after re-authorization (or by an operator) to re-check the DB at once."""
        self._wake = True

    @workflow.run
    async def run(self, inp: JobInput) -> str:
        cfg = inp.config
        job = JobRef(inp.tenant_id, inp.job_id)
        in_flight: set[str] = set(inp.in_flight)
        enumerated, stop_sent = inp.enumerated, inp.stop_sent
        self._cancel = self._cancel or inp.cancel_requested
        reconcile_children = bool(in_flight)  # right after continue-as-new: catch raced signals
        iterations = 0
        while True:
            try:
                # Patch "keep-early-wake" (M13): clear the wake flag BEFORE reading the DB, so a signal
                # that arrives while this iteration runs wakes the next wait at once. Old runs cleared it
                # just before waiting and could sleep a full poll interval after the last child finished.
                early_wake = workflow.patched("keep-early-wake")
                if early_wake:
                    self._wake = False
                if self._cancel and not stop_sent:
                    await _control(
                        cfg, "request_stop", StopRequest(job, "cancel", "cancel requested"), bool
                    )
                    stop_sent = True
                for key in self._finished:
                    in_flight.discard(key)
                self._finished.clear()
                if reconcile_children and in_flight:
                    closed = await _control(
                        cfg, "closed_children", ChildrenRequest(job, sorted(in_flight)), list[str]
                    )
                    in_flight.difference_update(closed)
                reconcile_children = False
                ov = await _control(
                    cfg,
                    "job_overview",
                    OverviewRequest(job, cfg.max_units_in_flight + len(in_flight)),
                    JobOverview,
                )
                running = ov.status == "running" and ov.stop_reason is None and not ov.sealed
                if ov.status in TERMINAL or ov.sealed:
                    break  # already finalized (e.g. a replayed finalize): finalize is idempotent
                if ov.stop_reason is not None and not in_flight:
                    break
                if running and not enumerated:
                    enumerated = await self._enumerate(cfg, job)
                    if enumerated:
                        continue  # start units at once; otherwise wait (paused/stopped/transient)
                if running and enumerated:
                    if not ov.remaining and not in_flight:
                        break
                    for key in ov.ready:
                        if len(in_flight) >= cfg.max_units_in_flight:
                            break
                        if key not in in_flight:
                            await self._start_unit(inp, cfg, key)
                            in_flight.add(key)
                timeout = cfg.job_poll_seconds
                if running and ov.next_retry_in is not None:
                    timeout = max(1.0, min(timeout, ov.next_retry_in + 1))
                if not early_wake:
                    self._wake = False
                try:
                    await workflow.wait_condition(
                        lambda: self._wake, timeout=timedelta(seconds=timeout)
                    )
                except TimeoutError:
                    reconcile_children = True  # periodic backstop
                iterations += 1
                if (
                    iterations >= cfg.job_iterations_per_run
                    or workflow.info().is_continue_as_new_suggested()
                ):
                    await workflow.wait_condition(workflow.all_handlers_finished)
                    for key in self._finished:
                        in_flight.discard(key)
                    workflow.continue_as_new(
                        JobInput(
                            inp.tenant_id,
                            inp.job_id,
                            cfg,
                            in_flight=sorted(in_flight),
                            enumerated=enumerated,
                            cancel_requested=self._cancel,
                            stop_sent=stop_sent,
                        )
                    )
            except asyncio.CancelledError:
                # a Temporal cancel is handled exactly like the cancel signal: cooperative, at batch
                # boundaries, then finalize as cancelled
                self._cancel = True
        return await _control(cfg, "finalize_job", job, str)

    @staticmethod
    async def _enumerate(cfg: RunConfig, job: JobRef) -> bool:
        try:
            await workflow.execute_activity(
                "enumerate_units",
                job,
                result_type=int,
                start_to_close_timeout=timedelta(seconds=cfg.start_to_close_seconds),
                heartbeat_timeout=timedelta(seconds=cfg.heartbeat_timeout_seconds),
                retry_policy=_retry(cfg),
            )
        except ActivityError as err:
            cls = error_class(err)
            error_type, message = error_text(err)
            if cls is ErrorClass.AUTH_REQUIRED:
                await _control(cfg, "pause_for_reauth", PauseRequest(job, message), int)
            elif cls is ErrorClass.TRANSIENT:
                return False  # tried again on the next poll
            elif cls is not ErrorClass.JOB_CLOSED:
                await _control(
                    cfg,
                    "request_stop",
                    StopRequest(job, "job_failure", f"enumeration: {error_type}: {message}"),
                    bool,
                )
            return False
        return True

    @staticmethod
    async def _start_unit(inp: JobInput, cfg: RunConfig, key: str) -> None:
        # a child for this unit may already be running: it is re-attached by its deterministic id
        with contextlib.suppress(WorkflowAlreadyStartedError):
            await workflow.start_child_workflow(
                CollectUnitWorkflow.run,
                UnitInput(inp.tenant_id, inp.job_id, key, cfg),
                id=unit_workflow_id(inp.job_id, key),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                parent_close_policy=ParentClosePolicy.ABANDON,
            )


@workflow.defn(name="MaintenanceWorkflow")
class MaintenanceWorkflow:
    """One sweeper run, started by a Temporal Schedule (overlap SKIP). A failure fails this run
    (visible in the UI); the next scheduled run tries again."""

    @workflow.run
    async def run(self, task: str) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity(
            task,
            result_type=dict[str, Any],
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=RetryPolicy(maximum_attempts=3, maximum_interval=timedelta(minutes=1)),
        )
        return result


@workflow.defn(name="ExportIngestWorkflow")
class ExportIngestWorkflow:
    """Hash and lock an uploaded Slack export, then validate it (ADR 0014; id ``export-{export_id}``).
    Both activities are idempotent and read their state from the DB, so a retry or a re-run after a
    crash continues where the export is."""

    @workflow.run
    async def run(self, ref: ExportRef) -> dict[str, Any]:
        options: dict[str, Any] = {
            "result_type": dict[str, Any],
            "start_to_close_timeout": timedelta(hours=12),
            "heartbeat_timeout": timedelta(seconds=ref.heartbeat_timeout_seconds),
            "retry_policy": RetryPolicy(
                initial_interval=timedelta(seconds=ref.retry_initial_seconds),
                maximum_interval=timedelta(seconds=ref.retry_max_seconds),
                maximum_attempts=ref.max_attempts,
            ),
        }
        locked: dict[str, Any] = await workflow.execute_activity("lock_export", ref, **options)
        if locked["status"] != "validating":
            return locked
        result: dict[str, Any] = await workflow.execute_activity("validate_export", ref, **options)
        return result
