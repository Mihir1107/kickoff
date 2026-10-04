"""Render test helpers."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_db.session import tenant_tx
from edisc_renderers.rsmf import RenderOptions, runtime_versions
from edisc_worker.renders import options_hash

Sessions = async_sessionmaker[AsyncSession]


async def register_render(
    sessions: Sessions,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    render_id: uuid.UUID,
    options: RenderOptions | None = None,
) -> uuid.UUID:
    """A ``renders`` row with exactly this id, for tests that drive ``render_and_store`` directly
    (productions reference their render). A live render with the same identity is failed first, the
    way a real second render of the same job and options can only exist after the first failed."""
    options = options or RenderOptions()
    async with tenant_tx(sessions, tenant_id) as s:
        if (await s.execute(text("SELECT 1 FROM renders WHERE id = :r"), {"r": render_id})).first():
            return render_id
        matter = (
            await s.execute(
                text("SELECT matter_id FROM collection_jobs WHERE id = :j"), {"j": job_id}
            )
        ).scalar_one()
        await s.execute(
            text(
                "UPDATE renders SET status = 'failed', reason = 'superseded_in_test', finished_at = now()"
                " WHERE job_id = :j AND options_hash = :h AND status IN ('requested', 'rendering', 'rendered')"
            ),
            {"j": job_id, "h": options_hash(options)},
        )
        await s.execute(
            text(
                "INSERT INTO renders (id, tenant_id, job_id, matter_id, options, options_hash,"
                " renderer_version, unicode_version, tzdata_version, requested_by)"
                " VALUES (:i, :t, :j, :m, CAST(:o AS jsonb), :h, :rv, :uv, :tv, 'tests')"
            ),
            {
                "i": render_id, "t": tenant_id, "j": job_id, "m": matter,
                "o": json.dumps(options.as_payload()), "h": options_hash(options),
                "rv": runtime_versions()["renderer_version"],
                "uv": runtime_versions()["unicode_version"],
                "tv": runtime_versions()["tzdata_version"],
            },
        )  # fmt: skip
    return render_id


# ------------------------------------------------------------------ step 4: renders end to end
async def new_render(
    sessions: Sessions,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    options: RenderOptions | None = None,
) -> uuid.UUID:
    """What the API does: a render row (deduplicated by identity)."""
    from edisc_worker.renders import create_render

    async with tenant_tx(sessions, tenant_id) as s:
        matter = (
            await s.execute(
                text("SELECT matter_id FROM collection_jobs WHERE id = :j"), {"j": job_id}
            )
        ).scalar_one()
        made = await create_render(
            s, tenant_id=tenant_id, job_id=job_id, matter_id=matter,
            options=options or RenderOptions(), requested_by="tests",
        )  # fmt: skip
    return made.render_id


async def drive(run: object, tenant_id: uuid.UUID, render_id: uuid.UUID) -> dict[str, object]:
    """The workflow's sequence, without Temporal: begin, files, complete."""
    from edisc_worker.renders import RenderRun

    assert isinstance(run, RenderRun)
    status = await run.begin(tenant_id, render_id)
    if status == "rendering":
        await run.render_files(tenant_id, render_id)
    return await run.complete(tenant_id, render_id)


async def expected_files(
    sessions: Sessions, s3: object, settings: object, tenant_id: uuid.UUID, job_id: uuid.UUID,
    options: RenderOptions | None = None,
) -> list[tuple[str, str, int]]:  # fmt: skip
    """Oracle: (name, SHA-256, size) of every file the job renders to, computed in memory with
    nothing stored, so stored bytes can be compared with an independent rendering."""
    import hashlib

    from edisc_renderers.rsmf import render_slice
    from edisc_worker.render_loader import RenderLoader

    options = options or RenderOptions()
    loader = RenderLoader(
        sessions,
        s3,
        settings,
        tenant_id=tenant_id,
        job_id=job_id,
        options=options,  # type: ignore[arg-type]
    )
    await loader.load_job()
    opener = loader.opener()
    out: list[tuple[str, str, int]] = []
    async for inp in loader.slices():
        for f in render_slice(inp, options):
            digest, size = hashlib.sha256(), 0
            async for chunk in f.astream(opener):
                digest.update(chunk)
                size += len(chunk)
            out.append((f.name, digest.hexdigest(), size))
    return out


async def render_state(
    sessions: Sessions, tenant_id: uuid.UUID, render_id: uuid.UUID
) -> dict[str, object]:
    async with tenant_tx(sessions, tenant_id) as s:
        row = (await s.execute(text("SELECT * FROM renders WHERE id = :r"), {"r": render_id})).one()
        events = (
            await s.execute(
                text(
                    "SELECT event_type, job_id, render_id, payload FROM custody_events"
                    " WHERE stream_id = :r ORDER BY seq"
                ),
                {"r": render_id},
            )
        ).all()
        files = (
            await s.execute(
                text(
                    "SELECT ord, name, sha256, size_bytes, evidence_object_id FROM render_files"
                    " WHERE render_id = :r ORDER BY ord"
                ),
                {"r": render_id},
            )
        ).all()
        productions = (
            await s.execute(
                text(
                    "SELECT state, count(*) FROM evidence_objects WHERE render_id = :r"
                    " AND kind = 'production' GROUP BY state"
                ),
                {"r": render_id},
            )
        ).all()
        audits = (
            await s.execute(
                text(
                    "SELECT event_type, payload FROM custody_events WHERE stream_id = :t"
                    " AND payload->>'render_id' = :r ORDER BY seq"
                ),
                {"t": tenant_id, "r": str(render_id)},
            )
        ).all()
    return {
        "row": row,
        "events": list(events),
        "types": [e.event_type for e in events],
        "files": [(f.name, f.sha256, f.size_bytes) for f in files],
        "file_rows": list(files),
        "productions": {p[0]: p[1] for p in productions},
        "audits": [a.event_type for a in audits],
    }
