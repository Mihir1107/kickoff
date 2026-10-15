"""The events of a job's live stream (`GET /v1/jobs/{id}/stream`, M17 step 5, plan §4).

The stream writes ONLY these models. They carry statuses, counts and ids (unit keys, conversation
ids: what `GET /v1/jobs/{id}/units` already shows under the same permission), never evidence
content: no message text, no file or conversation or user name, no custody payload. Their schemas
are published as components and listed in the operation's ``x-sse-events``, so
``scripts/api_compat.py`` fails a change to any of them (``edisc-sse-event-changed``).

Ids (the SSE ``id:`` line) follow the job's custody chain, which is ordered by commit for one job:
a ``units`` event's id is the sequence number of the last chain event it reports (``"<seq>"``); a
``job`` snapshot's id is ``"<head>.<digest>"``, the head it was taken at plus a digest of its content.
A reconnect with ``Last-Event-ID`` gets exactly the unit events after that sequence number, and the
snapshot only if it differs from the one the client last saw: nothing missed, nothing twice.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobEvent(Strict):
    """``event: job`` -- the job's state now."""

    status: str
    clean: bool
    clean_basis: str | None
    sealed: bool
    paused: bool
    units_by_status: dict[str, int]
    units_by_recon_status: dict[str, int]


class UnitState(Strict):
    seq: int  # the chain event that settled it (the job's custody sequence number)
    unit_key: str
    conversation_id: str
    day: date
    status: str
    recon_status: str
    expected_count: int | None
    collected_count: int
    file_gaps: int


class UnitsEvent(Strict):
    """``event: units`` -- units the chain recorded as settled (reconciled or failed), in chain
    order, at most ``MAX_UNITS`` per event."""

    units: list[UnitState]


class EndReason(StrEnum):
    SEALED = "sealed"  # the job's chain is sealed: nothing more will change
    SESSION_REVOKED = "session_revoked"
    SESSION_EXPIRED = "session_expired"  # idle or absolute: an open stream never extends a session
    PRINCIPAL_DEACTIVATED = "principal_deactivated"
    PERMISSION_LOST = "permission_lost"  # job.read no longer held on the job
    SLOW_CONSUMER = "slow_consumer"  # the client did not read; its bounded buffer overflowed
    LIFETIME = "lifetime"  # the stream's maximum lifetime: reconnect (and re-authenticate)


class EndEvent(Strict):
    """``event: end`` -- the last event of a stream, then the server closes it."""

    reason: EndReason


MAX_UNITS = 500
EVENTS: dict[str, type[Strict]] = {"job": JobEvent, "units": UnitsEvent, "end": EndEvent}
SCHEMAS = ("JobEvent", "UnitsEvent", "UnitState", "EndEvent", "EndReason")


def add_schemas(doc: dict[str, Any]) -> dict[str, Any]:
    """Publish the event models as components (the route's ``x-sse-events`` names them)."""
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    for model in (JobEvent, UnitsEvent, EndEvent):
        generated = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, sub in generated.pop("$defs", {}).items():
            schemas[name] = sub
        schemas[model.__name__] = generated
    return doc
