"""A job paused for re-authorization ends with its access lost, never open-ended (M17 step 8; ADR
0016 A6, A2.1, A2.2; plan §2.6, §8).

A pause (``Pipeline.pause_connection``) records its reason class, its deadline (the tenant's
``reauth_deadline_hours`` from the pause on; 24 h .. 90 days) and when to warn (24 h before the
deadline, or at half of it when the deadline is under 48 h), and raises one alert per job. From then
on exactly one of three things ends it:

- **re-authorization** before the deadline: the job resumes (``resume_paused_jobs``; ``ended_how``
  ``reauthorized``);
- **the deadline** passes (``check_reauth_deadlines``, a maintenance sweep);
- **finish without access**, the audited API action (``job.resume``, a reason, a recent sign-in).

The last two DECIDE first (``decide``: one transaction, the pause's ``ended_how`` / ``ended_by`` /
``end_reason`` and a ``job_pause_ended`` event in the job's chain), then CARRY OUT (``carry_out``):
every unit not yet reconciled closes as ``access_lost`` with the pause's reason class, EACH IN ITS
OWN TRANSACTION with its ``unit_reconciled`` event (ADR 0018 §28 format), and only after the last
one the pause ends (``resumed_at``), its alerts resolve and the job's ``stop_reason`` becomes
``access_lost``, so the workflow finalizes it (``completed_with_gaps``). A crash anywhere in between
leaves a decided pause the sweep finds again (``open_reauth_pauses``) and finishes: units already
closed are skipped, the job never finalizes with a unit unsettled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from edisc_connectors_base.types import AuthenticationError, Connection
from edisc_core.logs import get_logger
from edisc_custody.log import anchor_if_due, append
from edisc_db.session import tenant_tx
from edisc_worker.pipeline import ACTOR, Pipeline

log = get_logger(__name__)

DEADLINE_ACTOR = "system:reauth-deadline"
HOW = ("deadline", "finished_without_access")
UNIT_BATCH = 500  # unit keys read per round; each unit still closes in its own transaction


class NotPausedError(RuntimeError):
    """The job has no open, undecided pause (it is not waiting for re-authorization)."""


async def decide(
    s: AsyncSession, *, tenant_id: uuid.UUID, job_id: uuid.UUID, how: str, actor: str,
    reason: str | None,
) -> dict[str, Any]:  # fmt: skip
    """In the caller's transaction: the open pause of ``job_id`` ends ``how`` (``deadline`` or
    ``finished_without_access``), recorded on the pause and as ``job_pause_ended`` in the job's
    chain. Raises ``NotPausedError`` when there is no open undecided pause. Returns the event
    payload."""
    if how not in HOW:
        raise ValueError(f"unknown end {how!r}")
    pause = (
        await s.execute(
            text(
                "SELECT p.id, p.reason_class, p.paused_at, p.deadline_at, j.status FROM job_pauses p"
                " JOIN collection_jobs j ON j.id = p.job_id WHERE p.job_id = :j"
                " AND p.resumed_at IS NULL AND p.ended_how IS NULL FOR NO KEY UPDATE OF p"
            ),
            {"j": job_id},
        )
    ).one_or_none()
    if pause is None or pause.status != "paused_awaiting_reauth":
        raise NotPausedError(f"job {job_id} has no open pause awaiting re-authorization")
    await s.execute(
        text("UPDATE job_pauses SET ended_how = :h, ended_by = :a, end_reason = :r WHERE id = :p"),
        {"h": how, "a": actor, "r": reason, "p": pause.id},
    )
    payload = {
        "pause_id": str(pause.id), "ended_how": how,
        "reason_class": pause.reason_class or "credentials_revoked",
        "paused_at": pause.paused_at.isoformat(),
        "deadline_at": pause.deadline_at.isoformat() if pause.deadline_at else None,
        **({"reason": reason} if reason is not None else {}),
    }  # fmt: skip
    await append(s, tenant_id=tenant_id, stream_id=job_id, job_id=job_id,
                 event_type="job_pause_ended", actor=actor, payload=payload)  # fmt: skip
    return payload


async def carry_out(pipeline: Pipeline, *, tenant_id: uuid.UUID, job_id: uuid.UUID) -> int:
    """Close every unit of the job not yet reconciled as ``access_lost`` (one transaction each), then
    end the pause and set the job's stop reason. Idempotent and resumable. Returns the units closed
    by this call."""
    async with tenant_tx(pipeline.sessions, tenant_id) as s:
        pause = (
            await s.execute(
                text(
                    "SELECT id, reason_class FROM job_pauses WHERE job_id = :j"
                    " AND resumed_at IS NULL AND ended_how IS NOT NULL"
                ),
                {"j": job_id},
            )
        ).one_or_none()
    if pause is None:
        return 0  # nothing decided (or already carried out)
    cls = pause.reason_class or "credentials_revoked"
    closed = 0
    while True:
        async with tenant_tx(pipeline.sessions, tenant_id) as s:
            keys: list[str] = list((await s.execute(text(
                "SELECT unit_key FROM work_units WHERE job_id = :j AND status NOT IN ('done', 'failed')"
                " ORDER BY unit_key COLLATE \"C\" LIMIT :n"), {"j": job_id, "n": UNIT_BATCH})).scalars())  # fmt: skip
        if not keys:
            break
        for key in keys:
            closed += await _close_unit(pipeline, tenant_id, job_id, key, cls)
    async with tenant_tx(pipeline.sessions, tenant_id) as s:
        await s.execute(
            text("UPDATE job_pauses SET resumed_at = now() WHERE id = :p AND resumed_at IS NULL"),
            {"p": pause.id},
        )
        await resolve_job_alerts(s, job_id)
        await s.execute(
            text(
                "UPDATE collection_jobs SET stop_reason = 'access_lost', stop_requested_at = now()"
                " WHERE id = :j AND stop_reason IS NULL AND finished_at IS NULL"
            ),
            {"j": job_id},
        )
    await anchor_if_due(
        pipeline.sessions, pipeline.s3, pipeline.settings, tenant_id=tenant_id, stream_id=job_id
    )
    return closed


async def _close_unit(
    pipeline: Pipeline, tenant_id: uuid.UUID, job_id: uuid.UUID, unit_key: str, cls: str
) -> int:
    async with tenant_tx(pipeline.sessions, tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT unit_key, kind, conversation_id, day, status, expected_count, file_gaps,"
                    " access_lost_reason, gap_reasons FROM work_units WHERE job_id = :j"
                    " AND unit_key = :k FOR NO KEY UPDATE"
                ),
                {"j": job_id, "k": unit_key},
            )
        ).one()
        if row.status in ("done", "failed"):
            return 0  # another run closed it (or the unit finished before the decision)
        collected = (
            0
            if row.kind != "conversation_day"
            else len(await Pipeline._linked_messages(s, job_id, unit_key, row.day))
        )
        await s.execute(
            text(
                "UPDATE work_units SET status = 'done', recon_status = 'access_lost',"
                " access_lost_reason = :r, collected_count = :c, updated_at = now()"
                " WHERE job_id = :j AND unit_key = :k"
            ),
            {"r": cls, "c": collected, "j": job_id, "k": unit_key},
        )
        closed = SimpleNamespace(**{**row._mapping, "access_lost_reason": cls})
        detail = (await pipeline.unit_detail(s, job_id, [closed]))[unit_key]
        await append(
            s, tenant_id=tenant_id, stream_id=job_id, job_id=job_id,
            event_type="unit_reconciled", actor=ACTOR,
            payload={"unit_key": unit_key, "expected": row.expected_count, "collected": collected,
                     "recon_status": "access_lost", "file_gaps": row.file_gaps,
                     "no_longer_observed": 0, **detail},
            anchor_every=pipeline.anchor_every,
        )  # fmt: skip
    await pipeline.hooks.hit("access_lost_unit_closed")
    return 1


async def resolve_job_alerts(s: AsyncSession, job_id: uuid.UUID) -> None:
    """The pause's alerts are over (whatever ended it)."""
    await s.execute(
        text(
            "UPDATE alerts SET resolved_at = now() WHERE job_id = :j"
            " AND kind IN ('job_paused', 'reauth_deadline_near') AND resolved_at IS NULL"
        ),
        {"j": job_id},
    )


async def wake(temporal: Client | None, job_id: uuid.UUID) -> None:
    """Tell the job's workflow to look now (it also polls on its own: ADR 0012)."""
    if temporal is None:
        return
    try:
        await temporal.get_workflow_handle(str(job_id)).signal("wake")
    except RPCError as exc:
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise


@dataclass
class DeadlineSweep:
    closed_jobs: int = 0
    units_closed: int = 0
    warned: int = 0
    jobs: list[str] = field(default_factory=list)


async def check_reauth_deadlines(
    sweeper_sessions: async_sessionmaker[AsyncSession], pipeline: Pipeline,
    temporal: Client | None, *, tenant_id: uuid.UUID | None = None, limit: int = 200,
) -> DeadlineSweep:  # fmt: skip
    """The maintenance sweep: warn pauses whose warning is due (once), decide pauses past their
    deadline, and carry out every decided pause (including one an API request or an earlier run
    decided and did not finish)."""
    async with sweeper_sessions() as s, s.begin():
        rows = (
            await s.execute(
                text("SELECT tenant_id, job_id, pause_id FROM open_reauth_pauses(:n)"),
                {"n": limit},
            )
        ).all()
    out = DeadlineSweep()
    for r in rows:
        if tenant_id is not None and r.tenant_id != tenant_id:
            continue
        decided = False
        async with tenant_tx(pipeline.sessions, r.tenant_id) as s:
            p = (
                await s.execute(
                    text(
                        "SELECT id, connection_id, ended_how, deadline_at, warned_at,"
                        " deadline_at <= now() AS due, warn_at <= now() AS warn_due"
                        " FROM job_pauses WHERE id = :p AND resumed_at IS NULL FOR NO KEY UPDATE"
                    ),
                    {"p": r.pause_id},
                )
            ).one_or_none()
            if p is None:
                continue  # resumed meanwhile
            if p.ended_how is None and p.due:
                await decide(s, tenant_id=r.tenant_id, job_id=r.job_id, how="deadline",
                             actor=DEADLINE_ACTOR, reason=None)  # fmt: skip
                decided = True
            elif p.ended_how is None and p.warn_due and p.warned_at is None:
                await s.execute(
                    text("UPDATE job_pauses SET warned_at = now() WHERE id = :p"), {"p": p.id}
                )
                await s.execute(
                    text(
                        "INSERT INTO alerts (id, tenant_id, kind, connection_id, job_id, message)"
                        " VALUES (gen_random_uuid(), :t, 'reauth_deadline_near', :c, :j, :m)"
                    ),
                    {"t": r.tenant_id, "c": p.connection_id, "j": r.job_id,
                     "m": f"Job {r.job_id} finishes without access at {p.deadline_at.isoformat()}"
                          " unless its connection is re-authorized before then."},
                )  # fmt: skip
                out.warned += 1
                continue
            elif p.ended_how is None:
                continue
        if decided:
            await anchor_if_due(pipeline.sessions, pipeline.s3, pipeline.settings,
                                tenant_id=r.tenant_id, stream_id=r.job_id)  # fmt: skip
        out.units_closed += await carry_out(pipeline, tenant_id=r.tenant_id, job_id=r.job_id)
        out.closed_jobs += 1
        out.jobs.append(str(r.job_id))
        await wake(temporal, r.job_id)
    return out


@dataclass
class ConnectionsChecked:
    ok: int = 0
    revoked: int = 0
    unchecked: dict[str, int] = field(default_factory=dict)  # source -> not checkable here


async def check_connections(
    sweeper_sessions: async_sessionmaker[AsyncSession], pipeline: Pipeline,
    connectors: dict[str, Any], *, tenant_id: uuid.UUID | None = None, limit: int = 1000,
) -> ConnectionsChecked:  # fmt: skip
    """Plan §2.6: every active connection holding credentials, once per run, through its source's
    cheapest authenticated call (``Connector.check_credentials``, through the rate limiter). A
    rejection turns the connection ``reauth_required`` (its running jobs pause, an alert) before
    the next job finds out. A source without a connector in this worker is counted ``unchecked``
    (never reported as fine). ``credentials_checked_at`` is stamped BEFORE each call and the
    least recently attempted go first, so any other failure fails the run (retried by the
    schedule's policy) without starving the connections after it."""
    async with sweeper_sessions() as s, s.begin():
        rows = (
            await s.execute(
                text("SELECT tenant_id, connection_id, source FROM connections_to_check(:n)"),
                {"n": limit},
            )
        ).all()
    out = ConnectionsChecked()
    for r in rows:
        if tenant_id is not None and r.tenant_id != tenant_id:
            continue
        connector = connectors.get(r.source)
        if connector is None:
            out.unchecked[r.source] = out.unchecked.get(r.source, 0) + 1
            continue
        async with tenant_tx(pipeline.sessions, r.tenant_id) as s:
            c = (
                await s.execute(
                    text(
                        "UPDATE connections SET credentials_checked_at = now() WHERE id = :c"
                        " RETURNING id, source, external_org_id, config"
                    ),
                    {"c": r.connection_id},
                )
            ).one()
        conn = Connection(r.tenant_id, c.id, c.source, c.external_org_id, dict(c.config))
        try:
            await connector.check_credentials(conn)
        except AuthenticationError as exc:
            await pipeline.pause_connection(
                tenant_id=r.tenant_id, connection_id=r.connection_id, reason=str(exc)
            )
            out.revoked += 1
            continue
        out.ok += 1
    if out.unchecked:
        log.warning("connections not checkable by this worker", unchecked=out.unchecked)
    return out
