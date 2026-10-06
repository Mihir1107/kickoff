"""Render a sealed job and store the files as locked production evidence (ADR 0015 §7, M15 step 3).

Two passes over the job:

1. **Plan:** load (with every page verified), render the manifests and reconcile the whole job against
   its in-scope links. No byte is written, so a reconciliation failure leaves nothing behind.
2. **Write:** load and render again. Each file must match pass 1 (name, source hash, counts). Its
   bytes are streamed into WORM under `t/{tenant}/productions/{render}/` while hashed, and recorded
   with VersionId and our SHA-256. The rows are tied to the rendered job, so its matter owns their
   retention. File evidence streams through the zip by pinned version and is checked as it passes;
   a mismatch aborts that upload, and no object is created.

Attachments kept outside the zip (ADR 0015 §11, §20) are natives: pass 1 records which output files
reference each one (by SHA-256), and pass 2 copies each native server-side from its pinned evidence
version (``EvidenceWriter.write_native``: never through the worker, then one verification read)
BEFORE the first file that references it is written, so it exists before that file's batch commits.
The file is handed on with the natives it is the first to reference.

Pages verified in pass 1 are not read again in pass 2 (the loader remembers them for the render).
The render's custody stream and workflow (step 4) are in `edisc_worker.renders`: it passes `on_stored`,
which receives every stored file in render order, so files are committed in bounded batches and never
all held in memory.

Rendering a slice is CPU-bound (manifest, canonical JSON, schema validation: about 1.5 s for a
10,001-event slice on a laptop, several times that on a loaded host). It runs in a worker thread
(`_render_slice`), never on the event loop: the activity's heartbeats are sent from the loop, and a
loop blocked longer than the heartbeat timeout made Temporal time out a live attempt; every retry
re-rendered the same slice and timed out again, until the render failed (CI run 37412915073).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_evidence.writer import EvidenceWriter
from edisc_renderers.rsmf import (
    FileAttachment,
    Reconciler,
    Reconciliation,
    ReconciliationError,
    RenderedFile,
    RenderOptions,
    SliceInput,
    render_slice,
)
from edisc_worker.pipeline import CrashHooks
from edisc_worker.render_loader import LoadedJob, RenderLoader


@dataclass(frozen=True)
class StoredNative:
    ord: int  # position among the render's natives: order of first reference (file ord, SHA-256)
    sha256: str
    size: int
    evidence_id: uuid.UUID
    storage_key: str
    version_id: str
    file_ords: tuple[int, ...]  # every output file that references it, ascending
    deduplicated: bool

    def record(self) -> dict[str, Any]:
        """The native's record (``edisc_custody.render_files.NATIVE_FIELDS``)."""
        return {
            "ord": self.ord,
            "sha256": self.sha256,
            "size": self.size,
            "storage_key": self.storage_key,
            "version_id": self.version_id,
            "file_ords": list(self.file_ords),
        }


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
    natives: tuple[StoredNative, ...] = ()  # natives this file is the first to reference


@dataclass(frozen=True)
class RenderOutput:
    render_id: uuid.UUID
    job: LoadedJob
    options: RenderOptions
    files: tuple[StoredFile, ...]  # empty when the files went to ``on_stored``
    file_count: int
    reconciliation: Reconciliation
    verified_objects: int  # pages and archive entries read by pinned version and verified
    reconciler: Reconciler  # holds the natives the files reference (``check_natives``)
    native_count: int


async def _hooked(chunks: AsyncIterator[bytes], hooks: CrashHooks) -> AsyncIterator[bytes]:
    first = True
    async for chunk in chunks:
        yield chunk
        if first:
            first = False
            await hooks.hit("mid_upload")


def _render_slice_blocking(
    inp: SliceInput, options: RenderOptions, hooks: CrashHooks
) -> list[RenderedFile]:
    hooks.block("render_slice")  # test-only seam, synchronous like the rendering it stands for
    return render_slice(inp, options)


async def _render_slice(
    inp: SliceInput, options: RenderOptions, hooks: CrashHooks
) -> list[RenderedFile]:
    """`render_slice` off the event loop, so heartbeats keep flowing while it runs (module doc)."""
    return await asyncio.to_thread(_render_slice_blocking, inp, options, hooks)


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
    ``file_stored`` after a file is stored and before it is handed on; for natives
    ``native_parts_copied`` (before the copy completes), ``native_copied`` (after it, before the
    verification read), ``native_verifying`` (inside that read) and ``native_written``."""
    hooks = hooks or CrashHooks()
    loader = RenderLoader(
        sessions, s3, settings, tenant_id=tenant_id, job_id=job_id, options=options
    )
    job = await loader.load_job()

    # pass 1: plan and reconcile, write nothing
    reconciler = Reconciler(options)
    planned: list[dict[str, Any]] = []
    referenced: dict[str, list[int]] = {}  # native SHA-256 -> ords of the files that reference it
    async for inp in loader.slices():
        files = await _render_slice(inp, options, hooks)
        await asyncio.to_thread(reconciler.add_slice, inp, files)  # CPU, writes nothing: a thread
        for f in files:
            for digest in sorted({a.sha256 for a in f.externals}):
                referenced.setdefault(digest, []).append(len(planned))
            planned.append(_plan_key(f))
        await hooks.hit("planning")
    summary = reconciler.finish(job.expected_items, job.expected_digest)
    await hooks.hit("planned")

    # pass 2: render again (deterministic), compare with the plan, stream into WORM
    writer = EvidenceWriter(sessions, s3, settings)
    opener = loader.opener()
    stored: list[StoredFile] = []
    count = 0
    natives: dict[str, StoredNative] = {}

    async def native(a: FileAttachment) -> StoredNative:
        source_key, source_version = await loader.native_source(a)
        written = await writer.write_native(
            tenant_id=tenant_id, job_id=job_id, render_id=render_id, sha256=a.sha256,
            size=a.size, source_key=source_key, source_version_id=source_version,
            matter_retention_until=job.matter_retention_until, on=hooks.hit,
        )  # fmt: skip
        await hooks.hit("native_written")
        return StoredNative(
            ord=len(natives), sha256=a.sha256, size=a.size, evidence_id=written.evidence_id,
            storage_key=written.storage_key, version_id=written.version_id,
            file_ords=tuple(referenced[a.sha256]), deduplicated=written.deduplicated,
        )  # fmt: skip

    async for inp in loader.slices():
        for f in await _render_slice(inp, options, hooks):
            index = count
            if index >= len(planned) or _plan_key(f) != planned[index]:
                raise ReconciliationError(f"{f.name}: the second pass differs from the plan")
            first: list[StoredNative] = []
            for a in sorted(f.externals, key=lambda x: (x.sha256, x.file_id)):
                if a.sha256 not in natives:  # written before the first file that references it
                    if referenced.get(a.sha256, [None])[0] != index:
                        raise ReconciliationError(f"{f.name}: native {a.sha256} is not in the plan")
                    natives[a.sha256] = await native(a)
                    first.append(natives[a.sha256])
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
                natives=tuple(first),
            )
            count += 1
            if on_stored is None:
                stored.append(one)
            else:
                await on_stored(one)
    if count != len(planned):
        raise ReconciliationError(f"planned {len(planned)} files, wrote {count}")
    reconciler.check_natives((n.sha256, n.size) for n in natives.values())
    return RenderOutput(
        render_id, job, options, tuple(stored), count, summary, loader.verified_objects,
        reconciler, len(natives),
    )  # fmt: skip
