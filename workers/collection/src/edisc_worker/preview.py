"""The on-demand HTML preview of one conversation-day (ADR 0018 §1.2, §4.7, §10). Nothing is stored:
the API audits and anchors the page before its first byte.

`build_preview` checks the request (sealed job, a day inside a scope covering the conversation, a
known time zone), loads the day through the render loader's day-bounded entry
(`RenderLoader.day_slice`: every page and archive entry behind the day's items read by pinned version
and checked, as for a render), adds the facts the header states (the job's status verbatim, the units
covering the day), and renders the page in a worker thread (pure, writes nothing). Refusals carry a
code the route maps to its status; anything the loader finds inconsistent is an integrity failure.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.schemas import JobStatus
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_renderers import preview as preview_html
from edisc_renderers.preview import DayUnit, PreviewInput, PreviewJob, PreviewPage, PreviewPageError
from edisc_renderers.rsmf import RenderInputError, RenderOptions, slice_bounds
from edisc_worker.render_loader import RenderInputIntegrityError, RenderLoader


class PreviewRefusedError(Exception):
    """The request cannot be previewed: ``code`` is ``job_not_sealed``, ``not_in_scope``,
    ``page_not_found`` or ``unknown_time_zone``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class PreviewIntegrityError(Exception):
    """Stored evidence, registry rows or the loaded day disagree: always an incident (alert)."""


@dataclass(frozen=True)
class PreviewResult:
    page: PreviewPage
    matter_id: uuid.UUID


async def build_preview(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    conversation_id: str,
    day: date,
    time_zone: str,
    page: int,
) -> PreviewResult:
    try:
        options = RenderOptions(time_zone=time_zone)
    except RenderInputError as exc:
        raise PreviewRefusedError("unknown_time_zone", str(exc)) from exc
    start, end = slice_bounds(day, options.zone())
    async with tenant_tx(sessions, tenant_id) as s:
        job = (
            await s.execute(
                text(
                    "SELECT j.status, j.sealed_at, j.matter_id, c.source AS connection_source"
                    " FROM collection_jobs j JOIN connections c ON c.id = j.connection_id"
                    " WHERE j.id = :j"
                ),
                {"j": job_id},
            )
        ).one()
        if job.sealed_at is None:
            raise PreviewRefusedError("job_not_sealed", f"job {job_id} is {job.status}")
        ranges = (
            await s.execute(
                text(
                    "SELECT DISTINCT cs.date_from, cs.date_to FROM work_unit_scopes ws"
                    " JOIN work_units wu ON wu.job_id = ws.job_id AND wu.unit_key = ws.unit_key"
                    " JOIN collection_scopes cs ON cs.id = ws.scope_id"
                    " WHERE ws.job_id = :j AND wu.conversation_id = :c"
                ),
                {"j": job_id, "c": conversation_id},
            )
        ).all()
        if not any(r.date_from < end and r.date_to > start for r in ranges):
            # never an empty page for a day no scope covers (§4.7)
            raise PreviewRefusedError(
                "not_in_scope", f"{conversation_id} {day} ({time_zone}) is outside every scope"
            )
        utc_days = [
            start.date() + timedelta(days=i)
            for i in range(((end - timedelta(microseconds=1)).date() - start.date()).days + 1)
        ]
        units = (
            await s.execute(
                text(
                    "SELECT unit_key, status, recon_status, expected_count, collected_count,"
                    " file_gaps, access_lost_reason, gap_reasons FROM work_units"
                    " WHERE job_id = :j AND conversation_id = :c AND day = ANY(:d)"
                    " AND kind = 'conversation_day' ORDER BY unit_key"
                ),
                {"j": job_id, "c": conversation_id, "d": utc_days},
            )
        ).all()
    loader = RenderLoader(
        sessions, s3, settings, tenant_id=tenant_id, job_id=job_id, options=options
    )
    try:
        day_slice = await loader.day_slice(conversation_id, day)
    except (RenderInputIntegrityError, RenderInputError) as exc:
        raise PreviewIntegrityError(str(exc)) from exc
    status = JobStatus(job.status)
    inp = PreviewInput(
        job=PreviewJob(
            job_id=str(job_id),
            status=status.value,
            clean=status.is_clean,
            basis="archive" if job.connection_source == "slack_export" else "source",
        ),
        slice=day_slice,
        time_zone=time_zone,
        units=tuple(_unit(u) for u in units),
    )
    try:  # pure CPU work: in a thread (ADR 0015 §24); the cap is read at call time
        rendered = await asyncio.to_thread(
            preview_html.render_preview, inp, page, preview_html.PREVIEW_PAGE_EVENTS
        )
    except PreviewPageError as exc:
        raise PreviewRefusedError("page_not_found", str(exc)) from exc
    except RenderInputError as exc:
        raise PreviewIntegrityError(str(exc)) from exc
    return PreviewResult(rendered, job.matter_id)


def _unit(u: Any) -> DayUnit:
    return DayUnit(
        unit_key=u.unit_key,
        status=u.status,
        recon_status=u.recon_status,
        expected=u.expected_count,
        collected=u.collected_count,
        file_gaps=u.file_gaps,
        access_lost_reason=u.access_lost_reason,
        gap_reasons=tuple(u.gap_reasons or ()),
    )
