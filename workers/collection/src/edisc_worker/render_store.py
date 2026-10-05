"""Render a sealed job and store the files as locked production evidence (ADR 0015 §7, M15 step 3).

Two passes over the job:

1. **Plan:** load (with every page verified), render the manifests and reconcile the whole job against
   its in-scope links. No byte is written, so a reconciliation failure leaves nothing behind.
2. **Write:** load and render again. Each file must match pass 1 (name, source hash, counts). Its
   bytes are streamed into WORM under `t/{tenant}/productions/{render}/` while hashed, and recorded
   with VersionId and our SHA-256. The rows are tied to the rendered job, so its matter owns their
   retention. File evidence streams through the zip by pinned version and is checked as it passes;
   a mismatch aborts that upload, and no object is created.

Pages verified in pass 1 are not read again in pass 2 (the loader remembers them for the render).
The render's custody stream and workflow (step 4) are in `edisc_worker.renders`: it passes `on_stored`,
which receives every stored file in render order, so files are committed in bounded batches and never
all held in memory.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_evidence.writer import EvidenceWriter
from edisc_renderers.rsmf import (
    Reconciler,
    Reconciliation,
    ReconciliationError,
    RenderedFile,
    RenderOptions,
    render_slice,
)
from edisc_worker.pipeline import CrashHooks
from edisc_worker.render_loader import LoadedJob, RenderLoader


@dataclass(frozen=True)
class StoredFile:
    ord: int  # position in the render (deterministic: the same inputs give the same order)
    name: str
    evidence_id: uuid.UUID
    storage_key: str
    version_id: str
    sha256: str
    size: int
    record: dict[str, Any]  # RenderedFile.record(): slice, part, source hash, counts
    deduplicated: bool  # an earlier attempt had already stored exactly these bytes


@dataclass(frozen=True)
class RenderOutput:
    render_id: uuid.UUID
    job: LoadedJob
    options: RenderOptions
    files: tuple[StoredFile, ...]  # empty when the files went to ``on_stored``
    file_count: int
    reconciliation: Reconciliation
    verified_objects: int  # pages and archive entries read by pinned version and verified


async def _hooked(chunks: AsyncIterator[bytes], hooks: CrashHooks) -> AsyncIterator[bytes]:
    first = True
    async for chunk in chunks:
        yield chunk
        if first:
            first = False
            await hooks.hit("mid_upload")


def _plan_key(f: RenderedFile) -> dict[str, Any]:
    return f.record()


async def render_and_store(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    render_id: uuid.UUID,
    options: RenderOptions,
    on_stored: Callable[[StoredFile], Awaitable[None]] | None = None,
    hooks: CrashHooks | None = None,
) -> RenderOutput:
    """``hooks``: the crash-matrix seam (no-op in production): ``planning`` after each planned slice,
    ``planned`` between the passes, ``mid_upload`` inside a file's upload after its first chunk,
    ``file_stored`` after a file is stored and before it is handed on."""
    hooks = hooks or CrashHooks()
    loader = RenderLoader(
        sessions, s3, settings, tenant_id=tenant_id, job_id=job_id, options=options
    )
    job = await loader.load_job()

    # pass 1: plan and reconcile, write nothing
    reconciler = Reconciler(options)
    planned: list[dict[str, Any]] = []
    async for inp in loader.slices():
        files = render_slice(inp, options)
        reconciler.add_slice(inp, files)
        planned.extend(_plan_key(f) for f in files)
        await hooks.hit("planning")
    summary = reconciler.finish(job.expected_items, job.expected_digest)
    await hooks.hit("planned")

    # pass 2: render again (deterministic), compare with the plan, stream into WORM
    writer = EvidenceWriter(sessions, s3, settings)
    opener = loader.opener()
    stored: list[StoredFile] = []
    count = 0
    async for inp in loader.slices():
        for f in render_slice(inp, options):
            index = count
            if index >= len(planned) or _plan_key(f) != planned[index]:
                raise ReconciliationError(f"{f.name}: the second pass differs from the plan")
            written = await writer.write_production(
                tenant_id=tenant_id,
                job_id=job_id,
                render_id=render_id,
                name=f.name,
                matter_retention_until=job.matter_retention_until,
                stream=lambda f=f: _hooked(f.astream(opener), hooks),  # type: ignore[misc]
            )
            await hooks.hit("file_stored")
            one = StoredFile(
                ord=index,
                name=f.name,
                evidence_id=written.evidence_id,
                storage_key=written.storage_key,
                version_id=written.version_id,
                sha256=written.sha256,
                size=written.size,
                record=f.record(),
                deduplicated=written.deduplicated,
            )
            count += 1
            if on_stored is None:
                stored.append(one)
            else:
                await on_stored(one)
    if count != len(planned):
        raise ReconciliationError(f"planned {len(planned)} files, wrote {count}")
    return RenderOutput(
        render_id, job, options, tuple(stored), count, summary, loader.verified_objects
    )
