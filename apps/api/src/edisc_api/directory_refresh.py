"""Start a directory capture right after a connection is created or re-authorized (M17 plan §3.3).

It starts the ``refresh-directories`` maintenance run under the id
``directory-refresh-<connection>``: the run captures every connection that is due, this one
included (no snapshot yet, or credentials replaced since the last one). Best effort by design:
nothing is lost if Temporal does not answer, because the schedule runs the same sweep every 15
minutes and the connection stays due until captured.
"""

from __future__ import annotations

import contextlib
import uuid

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from edisc_core.logs import get_logger
from edisc_worker.contracts import MAINTENANCE_QUEUE
from edisc_worker.workflows import MaintenanceWorkflow

log = get_logger(__name__)


async def start(temporal: Client, connection_id: uuid.UUID) -> None:
    try:
        with contextlib.suppress(WorkflowAlreadyStartedError):
            await temporal.start_workflow(
                MaintenanceWorkflow.run, "refresh_directories",
                id=f"directory-refresh-{connection_id}", task_queue=MAINTENANCE_QUEUE,
            )  # fmt: skip
    except RPCError as exc:  # the schedule captures it within its interval
        log.warning("directory refresh not started", connection_id=str(connection_id),
                    error=type(exc).__name__)  # fmt: skip
