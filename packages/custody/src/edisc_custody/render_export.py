"""Render packages (``edisc-render-package/2``, ADR 0015 §14 and §19): one generator for the directory
export and the download stream, verified offline by ``edisc-verify`` (``edisc_custody.render_package``).

Two passes, and no object is read twice:

1. ``plan_render_package`` builds the manifest from what was recorded and verified when the render was
   made: its custody events and ``render_files`` records (database), its anchors as LISTED from the
   bucket's object versions (never the database) with the SHA-256 and size the registry recorded when
   each was written, and the job seal anchor ``render_started`` references. Reading the JSONL content
   once here gives the manifest its file hashes; no object body is read (except, once, an anchor
   version the registry does not hold: a storage incident the verifier must see).
2. ``package_members`` produces the entries in a fixed order (manifest, JSONL files, objects by hash,
   outputs in render order), regenerating each from the same records, bounded by the planned head and
   file count, and checking every entry against the plan AS IT PASSES: the JSONL files against the
   manifest hashes, every object (anchors, the seal, embedded outputs) read by its pinned VersionId
   against its recorded SHA-256 and size. Any difference raises ``PackageIntegrityError``.

``export_render_package`` writes the members to a directory; the API streams them through
``edisc_custody.zipwriter`` as one deterministic zip. The manifest has no export time (the seal time
instead), so two packages of one render in one mode are byte-identical.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_core.time import format_utc
from edisc_custody.chain import anchor_prefix
from edisc_custody.log import event_record_from_row
from edisc_custody.render_files import RENDER_STARTED
from edisc_custody.render_package import RENDER_PACKAGE_FORMAT
from edisc_custody.zipwriter import ZipMember, ZipSizer
from edisc_db.session import tenant_tx
from edisc_evidence.worm import get_bytes, list_versions

Outputs = Literal["embed", "reference"]
CHUNK = 1 << 20
_LINES_CHUNK = 64 << 10
_JSONL_ORDER = ("events.jsonl", "files.jsonl", "anchors.jsonl", "job_seal.json")


class PackageIntegrityError(Exception):
    """What is being packaged differs from the plan (the records verified at seal). The package or
    download is aborted."""

    def __init__(self, entry: str, detail: str) -> None:
        super().__init__(f"{entry}: {detail}")
        self.entry, self.detail = entry, detail


@dataclass(frozen=True)
class PackageObject:
    key: str
    version_id: str
    sha256: str
    size: int


@dataclass(frozen=True)
class RenderPackagePlan:
    tenant_id: uuid.UUID
    render_id: uuid.UUID
    job_id: uuid.UUID
    outputs: Outputs
    manifest: bytes
    head_seq: int  # the events and files the package holds: never more than when it was planned
    file_count: int
    files: dict[str, dict[str, Any]]  # JSONL file -> sha256, lines, bytes (as in the manifest)
    anchors: tuple[dict[str, Any], ...]
    seal: dict[str, Any]
    objects: tuple[PackageObject, ...]  # anchor and seal bodies, by SHA-256
    page_size: int = 1000
    zip_size: int = 0  # the exact size of the zip of these members (Content-Length)

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.manifest).hexdigest()


# ------------------------------------------------------------------ records (bounded pages)
async def _event_lines(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    render_id: uuid.UUID,
    head_seq: int,
    page_size: int,
) -> AsyncIterator[dict[str, Any]]:
    after = 0
    while after < head_seq:
        async with tenant_tx(sessions, tenant_id) as s:  # one short transaction per page
            rows = (
                await s.execute(
                    text(
                        "SELECT id, tenant_id, stream_id, job_id, seq, event_type, actor, item_id, payload,"
                        " prev_hash, event_hash, created_at FROM custody_events WHERE stream_id = :r"
                        " AND seq > :a AND seq <= :h ORDER BY seq LIMIT :n"
                    ),
                    {"r": render_id, "a": after, "h": head_seq, "n": page_size},
                )
            ).all()
        if not rows:
            return
        for row in rows:
            ev = event_record_from_row(row)
            yield {"id": ev.id, "fields": ev.fields, "prev_hash": ev.prev_hash,
                   "event_hash": ev.event_hash}  # fmt: skip
        after = rows[-1].seq


async def _file_rows(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    render_id: uuid.UUID,
    file_count: int,
    page_size: int,
) -> AsyncIterator[Any]:
    last = -1
    while last + 1 < file_count:
        async with tenant_tx(sessions, tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT rf.ord, rf.record, rf.custody_event_id, rf.sha256, rf.size_bytes,"
                        " e.storage_key, e.version_id AS registry_version, e.sha256 AS registry_sha,"
                        " e.state, rf.version_id FROM render_files rf"
                        " JOIN evidence_objects e ON e.id = rf.evidence_object_id"
                        " WHERE rf.render_id = :r AND rf.ord > :a AND rf.ord < :c ORDER BY rf.ord LIMIT :n"
                    ),
                    {"r": render_id, "a": last, "c": file_count, "n": page_size},
                )
            ).all()
        if not rows:
            return
        for row in rows:
            yield row
        last = rows[-1].ord


def _file_line(row: Any) -> dict[str, Any]:
    return {"custody_event_id": str(row.custody_event_id), "record": dict(row.record)}


async def _jsonl_spec(lines: AsyncIterator[dict[str, Any]]) -> dict[str, Any]:
    digest, count, size = hashlib.sha256(), 0, 0
    async for obj in lines:
        line = canonical_json(obj) + b"\n"
        digest.update(line)
        count += 1
        size += len(line)
    return {"sha256": digest.hexdigest(), "lines": count, "bytes": size}


async def _registry(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, keys: list[str]
) -> dict[tuple[str, str], tuple[str, int]]:
    """(key, VersionId) -> (SHA-256, size) as recorded when each object was written."""
    async with tenant_tx(sessions, tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT storage_key, version_id, sha256, size_bytes FROM evidence_objects"
                    " WHERE storage_key = ANY(:k) AND state = 'complete'"
                ),
                {"k": keys},
            )
        ).all()
    return {(r.storage_key, r.version_id): (r.sha256, r.size_bytes) for r in rows}


# ------------------------------------------------------------------ pass 1: the plan
async def plan_render_package(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    render_id: uuid.UUID,
    outputs: Outputs = "embed",
    page_size: int = 1000,
) -> RenderPackagePlan:
    bucket = settings.s3_evidence_bucket
    async with tenant_tx(sessions, tenant_id) as s:
        render = (
            await s.execute(
                text(
                    "SELECT r.job_id, r.finished_at, r.sealed_at, h.last_seq, h.last_hash,"
                    " (SELECT count(*) FROM render_files f WHERE f.render_id = r.id) AS files"
                    " FROM renders r LEFT JOIN custody_chain_heads h ON h.stream_id = r.id"
                    " WHERE r.id = :r"
                ),
                {"r": render_id},
            )
        ).one()
    head_seq = int(render.last_seq or 0)
    file_count = int(render.files)

    versions = [
        v
        async for v in list_versions(
            s3, bucket=bucket, prefix=anchor_prefix(str(tenant_id), str(render_id))
        )
    ]
    reference: dict[str, Any] | None = None
    async for line in _event_lines(sessions, tenant_id, render_id, 1, page_size):
        if line["fields"]["event_type"] == RENDER_STARTED:  # (render_refused references no seal)
            reference = dict(line["fields"]["payload"]["job"])

    seal_ref = reference["seal"] if reference is not None else None
    keys = [v.key for v in versions if not v.is_delete_marker]
    registry = await _registry(
        sessions, tenant_id, [*keys, *([seal_ref["key"]] if seal_ref else [])]
    )

    async def described(key: str, version_id: str) -> PackageObject:
        known = registry.get((key, version_id))
        if known is None:  # not recorded (a shadow version, an incident): hashed once, here
            body = await get_bytes(s3, bucket=bucket, key=key, version_id=version_id)
            known = (hashlib.sha256(body).hexdigest(), len(body))
        return PackageObject(key, version_id, known[0], known[1])

    anchors: list[dict[str, Any]] = []
    objects: dict[str, PackageObject] = {}
    for v in versions:
        if v.is_delete_marker:
            anchors.append({"key": v.key, "version_id": v.version_id, "delete_marker": True})
            continue
        obj = await described(v.key, v.version_id)
        objects.setdefault(obj.sha256, obj)
        anchors.append({"key": v.key, "version_id": v.version_id, "sha256": obj.sha256,
                        "size": obj.size})  # fmt: skip
    seal: dict[str, Any] = {"key": None, "version_id": None, "sha256": None, "size": None}
    if seal_ref is not None:
        obj = await described(seal_ref["key"], seal_ref["version_id"])
        objects.setdefault(obj.sha256, obj)
        seal = {"key": obj.key, "version_id": obj.version_id, "sha256": obj.sha256,
                "size": obj.size}  # fmt: skip

    output_sizes: list[tuple[str, int]] = []  # name, recorded size: the zip's size before any byte

    async def files_lines() -> AsyncIterator[dict[str, Any]]:
        async for row in _file_rows(sessions, tenant_id, render_id, file_count, page_size):
            output_sizes.append((f"outputs/{row.record['name']}", int(row.size_bytes)))
            yield _file_line(row)

    files = {
        "events.jsonl": await _jsonl_spec(
            _event_lines(sessions, tenant_id, render_id, head_seq, page_size)
        ),
        "files.jsonl": await _jsonl_spec(files_lines()),
        "anchors.jsonl": await _jsonl_spec(_aiter(anchors)),
        "job_seal.json": await _jsonl_spec(_aiter([seal])),
    }
    manifest = {
        "format": RENDER_PACKAGE_FORMAT,
        "tenant_id": str(tenant_id),
        "render_id": str(render_id),
        "job_id": str(render.job_id),
        "finalized": render.finished_at is not None,
        "head": {"seq": render.last_seq, "hash": render.last_hash} if render.last_seq else None,
        "sealed_at": format_utc(render.sealed_at) if render.sealed_at else None,
        "outputs_included": outputs == "embed",
        "files": files,
    }
    manifest_bytes = canonical_json(manifest)
    ordered = [("manifest.json", len(manifest_bytes))]  # package_members' order, exactly
    ordered += [(name, files[name]["bytes"]) for name in _JSONL_ORDER]
    ordered += [(f"objects/{sha}", objects[sha].size) for sha in sorted(objects)]
    if outputs == "embed":
        ordered += output_sizes
    sizer = ZipSizer()
    for name, size in ordered:
        sizer.add(name, size)
    return RenderPackagePlan(
        tenant_id=tenant_id,
        render_id=render_id,
        job_id=render.job_id,
        outputs=outputs,
        manifest=manifest_bytes,
        head_seq=head_seq,
        file_count=file_count,
        files=files,
        anchors=tuple(anchors),
        seal=seal,
        objects=tuple(objects[sha] for sha in sorted(objects)),
        page_size=page_size,
        zip_size=sizer.total(),
    )


async def _aiter(items: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        yield item


# ------------------------------------------------------------------ pass 2: the members
def _jsonl_member(
    name: str, spec: dict[str, Any], lines: Callable[[], AsyncIterator[dict[str, Any]]]
) -> ZipMember:
    async def chunks() -> AsyncIterator[bytes]:
        digest, count, size, buf = hashlib.sha256(), 0, 0, bytearray()
        async for obj in lines():
            line = canonical_json(obj) + b"\n"
            digest.update(line)
            count += 1
            size += len(line)
            if size > spec["bytes"]:
                raise PackageIntegrityError(name, "longer than the manifest records")
            buf += line
            if len(buf) >= _LINES_CHUNK:
                yield bytes(buf)
                buf.clear()
        if (digest.hexdigest(), count, size) != (spec["sha256"], spec["lines"], spec["bytes"]):
            raise PackageIntegrityError(name, "differs from the manifest built from the records")
        if buf:
            yield bytes(buf)

    return ZipMember(name, spec["bytes"], chunks)


def _object_member(
    s3: S3Client, bucket: str, name: str, key: str, version_id: str, sha256: str, size: int
) -> ZipMember:
    async def chunks() -> AsyncIterator[bytes]:
        digest, seen = hashlib.sha256(), 0
        resp = await s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)  # PINNED version
        async with resp["Body"] as body:
            async for chunk in body.iter_chunks(CHUNK):
                seen += len(chunk)
                if seen > size:
                    raise PackageIntegrityError(name, f"more than the recorded {size} bytes")
                digest.update(chunk)
                yield chunk
        if (digest.hexdigest(), seen) != (sha256, size):
            raise PackageIntegrityError(
                name, f"read {seen} bytes sha256 {digest.hexdigest()}, recorded {size} {sha256}"
            )

    return ZipMember(name, size, chunks)


async def package_members(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    plan: RenderPackagePlan,
) -> AsyncIterator[ZipMember]:
    """The package entries in their fixed order, each verified against the plan as it streams."""
    t, r, n = plan.tenant_id, plan.render_id, plan.page_size
    bucket = settings.s3_evidence_bucket

    async def manifest() -> AsyncIterator[bytes]:
        yield plan.manifest

    async def files_lines() -> AsyncIterator[dict[str, Any]]:
        async for row in _file_rows(sessions, t, r, plan.file_count, n):
            yield _file_line(row)

    yield ZipMember("manifest.json", len(plan.manifest), manifest)
    yield _jsonl_member(
        "events.jsonl", plan.files["events.jsonl"],
        lambda: _event_lines(sessions, t, r, plan.head_seq, n),
    )  # fmt: skip
    yield _jsonl_member("files.jsonl", plan.files["files.jsonl"], files_lines)
    yield _jsonl_member(
        "anchors.jsonl", plan.files["anchors.jsonl"], lambda: _aiter(list(plan.anchors))
    )
    yield _jsonl_member("job_seal.json", plan.files["job_seal.json"], lambda: _aiter([plan.seal]))
    for obj in plan.objects:
        yield _object_member(
            s3, bucket, f"objects/{obj.sha256}", obj.key, obj.version_id, obj.sha256, obj.size
        )
    if plan.outputs != "embed":
        return
    async for row in _file_rows(sessions, t, r, plan.file_count, n):
        name = f"outputs/{row.record['name']}"
        if (
            row.state != "complete"
            or row.registry_sha != row.sha256
            or row.registry_version != row.version_id
        ):
            raise PackageIntegrityError(name, "the registry disagrees with the render's record")
        yield _object_member(
            s3, bucket, name, row.storage_key, row.version_id, row.sha256, row.size_bytes
        )


# ------------------------------------------------------------------ the directory export
async def export_render_package(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    render_id: uuid.UUID,
    dest: Path,
    outputs: Outputs = "embed",
    page_size: int = 1000,
) -> Path:
    """Write the package of one render into ``dest`` (must not exist): the same entries, with the
    same bytes, as the zip the download endpoint streams."""
    plan = await plan_render_package(
        sessions, s3, settings, tenant_id=tenant_id, render_id=render_id, outputs=outputs,
        page_size=page_size,
    )  # fmt: skip
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=False)
    async for member in package_members(sessions, s3, settings, plan):
        path = dest / member.name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            async for chunk in member.chunks():
                fh.write(chunk)
    return dest
