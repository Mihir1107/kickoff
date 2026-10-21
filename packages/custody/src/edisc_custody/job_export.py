"""Job custody packages (``edisc-custody-package/3``, ADR 0008, ADR 0018 §27): one generator for the
directory export (``edisc_custody.export.export_package``) and the download stream
(``GET /v1/jobs/{id}/package``), verified offline by ``edisc-verify`` (``edisc_verify.package``).

Format 3 is format 2 with a DETERMINISTIC manifest: the job's seal time instead of an export time,
so two packages of one sealed job in one mode are byte-identical. Same two passes as render packages
(``edisc_custody.render_export``), and no object body is read twice:

1. ``plan_job_package`` regenerates every JSONL file from the records (custody events up to the head,
   the items of each batch event, the evidence registry with every other version at each key, the
   anchors as LISTED from the bucket) to hash and size it, and lists the objects to embed by the
   SHA-256 and size the registry recorded. No evidence body is read (anchor bodies are small and go
   into ``anchors.jsonl``). The plan knows the zip's exact size (``Content-Length``).
2. ``job_package_members`` produces the entries in a fixed order (manifest, the four JSONL files, the
   objects by SHA-256), regenerating each file from the same records and checking it against the plan
   as it passes, and reading every object by its PINNED VersionId against its recorded SHA-256 and
   size (``PackageIntegrityError`` on any difference).
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_core.time import format_utc
from edisc_custody.log import event_record_from_row
from edisc_custody.render_export import (
    PackageIntegrityError,
    PackageObject,
    _jsonl_member,
    _jsonl_spec,
    _object_member,
)
from edisc_custody.zipwriter import ZipMember, ZipSizer
from edisc_db.session import tenant_tx
from edisc_evidence.worm import get_bytes, list_versions
from edisc_verify.canonical import canonical_json
from edisc_verify.chain import BATCH_EVENT, anchor_prefix
from edisc_verify.package import FILES, PACKAGE_FORMAT

Objects = Literal["embed", "omit"]
Archives = Literal["embed", "reference"]

__all__ = ["JobPackagePlan", "PackageIntegrityError", "job_package_members", "plan_job_package"]

_EVIDENCE_COLUMNS = (
    "id, storage_key, kind, state, sha256, size_bytes, version_id, source_sha256,"
    " source_hash_origin, archive_evidence_id, entry_path, entry_raw_name, entry_crc32,"
    " entry_compressed_size"
)
# the job's own objects, plus content-addressed files first stored by another job and shared by
# dedup (found through this job's items), and the archives (export zips) holding any of them
_EVIDENCE_OF_JOB = (
    "(job_id = :j OR id IN (SELECT i.evidence_object_id FROM job_items ji"
    " JOIN items i ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id WHERE ji.job_id = :j)"
    " OR id IN (SELECT e.archive_evidence_id FROM evidence_objects e WHERE e.job_id = :j"
    " OR e.id IN (SELECT i.evidence_object_id FROM job_items ji JOIN items i"
    " ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id WHERE ji.job_id = :j)))"
)


@dataclass(frozen=True)
class JobPackagePlan:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    objects_mode: Objects
    archives_mode: Archives
    manifest: bytes
    head_seq: int  # the events the package holds: never more than when it was planned
    files: dict[str, dict[str, Any]]  # JSONL file -> sha256, lines, bytes
    anchors: tuple[dict[str, Any], ...]
    objects: tuple[PackageObject, ...]  # embedded bodies, by SHA-256
    page_size: int = 1000
    zip_size: int = 0
    archives: tuple[dict[str, Any], ...] = field(default=())

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.manifest).hexdigest()


def _str(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value


def _evidence_record(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.pop("entry_raw_name", None)
    if row.get("kind") != "archive_entry":
        for key in ("archive_evidence_id", "entry_path", "entry_crc32", "entry_compressed_size"):
            row.pop(key, None)
    else:
        row["entry_raw_name_b64"] = base64.b64encode(bytes(raw)).decode()
    return {k: _str(v) for k, v in row.items()}


# ------------------------------------------------------------------ records (bounded pages)
async def _event_rows(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, job_id: uuid.UUID,
    head_seq: int, page_size: int,
) -> AsyncIterator[list[Any]]:  # fmt: skip
    after = 0
    while after < head_seq:
        async with tenant_tx(sessions, tenant_id) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id, tenant_id, stream_id, job_id, seq, event_type, actor, item_id,"
                        " payload, prev_hash, event_hash, created_at FROM custody_events"
                        " WHERE stream_id = :j AND seq > :a AND seq <= :h ORDER BY seq LIMIT :n"
                    ),
                    {"j": job_id, "a": after, "h": head_seq, "n": page_size},
                )
            ).all()
        if not rows:
            return
        yield list(rows)
        after = rows[-1].seq


async def _event_lines(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, job_id: uuid.UUID,
    head_seq: int, page_size: int,
) -> AsyncIterator[dict[str, Any]]:  # fmt: skip
    async for rows in _event_rows(sessions, tenant_id, job_id, head_seq, page_size):
        for row in rows:
            ev = event_record_from_row(row)
            yield {"id": ev.id, "fields": ev.fields, "prev_hash": ev.prev_hash,
                   "event_hash": ev.event_hash}  # fmt: skip


async def _item_lines(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, job_id: uuid.UUID,
    head_seq: int, page_size: int,
) -> AsyncIterator[dict[str, Any]]:  # fmt: skip
    """The items of every batch event, in event order then idempotency key (as the directory export
    always wrote them)."""
    async for rows in _event_rows(sessions, tenant_id, job_id, head_seq, page_size):
        for row in rows:
            if row.event_type != BATCH_EVENT:
                continue
            async with tenant_tx(sessions, tenant_id) as s:
                items = (
                    await s.execute(
                        text(
                            "SELECT i.id, i.source, i.source_item_id, i.version, i.item_type,"
                            " i.event_kind, i.idempotency_key, i.content_hash, i.raw_hash,"
                            " i.evidence_object_id, i.storage_key, i.json_path, ji.custody_event_id,"
                            " ji.unit_key FROM job_items ji JOIN items i ON i.tenant_id ="
                            " ji.tenant_id AND i.id = ji.item_id WHERE ji.custody_event_id = :e"
                            " ORDER BY i.idempotency_key"
                        ),
                        {"e": row.id},
                    )
                ).all()
            for it in items:
                yield {k: _str(v) for k, v in it._mapping.items()}


async def _evidence_rows(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, job_id: uuid.UUID,
    page_size: int,
) -> AsyncIterator[dict[str, Any]]:  # fmt: skip
    after: tuple[str, uuid.UUID] | None = None
    while True:
        cond = "" if after is None else " AND (storage_key, id) > (:k, :i)"
        params: dict[str, Any] = {"j": job_id, "n": page_size}
        if after is not None:
            params |= {"k": after[0], "i": after[1]}
        sql = (
            f"SELECT {_EVIDENCE_COLUMNS} FROM evidence_objects WHERE {_EVIDENCE_OF_JOB}{cond}"  # noqa: S608
            " ORDER BY storage_key, id LIMIT :n"
        )
        async with tenant_tx(sessions, tenant_id) as s:
            rows = (await s.execute(text(sql), params)).all()
        if not rows:
            return
        for r in rows:
            yield _evidence_record(dict(r._mapping))
        after = (rows[-1].storage_key, rows[-1].id)


async def _evidence_lines(
    sessions: async_sessionmaker[AsyncSession], s3: S3Client, bucket: str,
    tenant_id: uuid.UUID, job_id: uuid.UUID, page_size: int, seen: list[dict[str, Any]] | None,
) -> AsyncIterator[dict[str, Any]]:  # fmt: skip
    """Each registry record with every OTHER version at its key (shadows are storage incidents the
    expert must see). ``seen`` collects the records (the plan's pass only)."""
    async for rec in _evidence_rows(sessions, tenant_id, job_id, page_size):
        shadows = sorted(
            [
                v.version_id
                async for v in list_versions(s3, bucket=bucket, prefix=rec["storage_key"])
                if v.key == rec["storage_key"] and v.version_id != rec["version_id"]
            ]
        )
        if seen is not None:
            seen.append({k: rec[k] for k in ("id", "storage_key", "kind", "state", "sha256",
                                             "size_bytes", "version_id")})  # fmt: skip
        yield {**rec, "shadow_versions": shadows}


async def _aiter(items: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        yield item


# ------------------------------------------------------------------ pass 1: the plan
async def plan_job_package(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    objects: Objects = "embed",
    archives: Archives = "embed",
    page_size: int = 1000,
) -> JobPackagePlan:
    bucket = settings.s3_evidence_bucket
    async with tenant_tx(sessions, tenant_id) as s:
        job = (
            await s.execute(
                text(
                    "SELECT j.finished_at IS NOT NULL AS finished, j.sealed_at, h.last_seq,"
                    " h.last_hash FROM collection_jobs j"
                    " LEFT JOIN custody_chain_heads h ON h.stream_id = j.id WHERE j.id = :j"
                ),
                {"j": job_id},
            )
        ).one()
    head_seq = int(job.last_seq or 0)

    anchors: list[dict[str, Any]] = []
    async for v in list_versions(
        s3, bucket=bucket, prefix=anchor_prefix(str(tenant_id), str(job_id))
    ):
        record: dict[str, Any] = {"key": v.key, "version_id": v.version_id}
        if v.is_delete_marker:
            record["delete_marker"] = True
        else:
            body = await get_bytes(s3, bucket=bucket, key=v.key, version_id=v.version_id)
            record["body_b64"] = base64.b64encode(body).decode()
        anchors.append(record)

    registry: list[dict[str, Any]] = []
    files = {
        "events.jsonl": await _jsonl_spec(
            _event_lines(sessions, tenant_id, job_id, head_seq, page_size)
        ),
        "items.jsonl": await _jsonl_spec(
            _item_lines(sessions, tenant_id, job_id, head_seq, page_size)
        ),
        "evidence.jsonl": await _jsonl_spec(
            _evidence_lines(sessions, s3, bucket, tenant_id, job_id, page_size, registry)
        ),
        "anchors.jsonl": await _jsonl_spec(_aiter(anchors)),
    }
    archive_ids = {
        r["archive_evidence_id"] for r in await _archive_refs(sessions, tenant_id, job_id)
    }
    limits = await _archive_limits(sessions, tenant_id, archive_ids)
    embedded: dict[str, PackageObject] = {}
    archive_list = []
    for r in registry:
        is_archive = str(r["id"]) in archive_ids
        if is_archive:
            archive_list.append({
                "evidence_id": str(r["id"]), "storage_key": r["storage_key"],
                "version_id": r["version_id"], "sha256": r["sha256"],
                "size_bytes": r["size_bytes"],
                "embedded": objects == "embed" and archives == "embed",
                "limits": limits.get(str(r["id"])),
            })  # fmt: skip
        take = (is_archive and objects == "embed" and archives == "embed") or (
            not is_archive and objects == "embed" and r["state"] == "complete"
            and r["kind"] in ("page", "file")
        )  # fmt: skip
        if take:
            embedded.setdefault(
                r["sha256"],
                PackageObject(r["storage_key"], r["version_id"], r["sha256"], int(r["size_bytes"])),
            )
    manifest = {
        "format": PACKAGE_FORMAT,
        "tenant_id": str(tenant_id),
        "stream_id": str(job_id),
        "job_id": str(job_id),
        "finalized": bool(job.finished),
        "head": {"seq": job.last_seq, "hash": job.last_hash} if job.last_seq else None,
        "sealed_at": format_utc(job.sealed_at) if job.sealed_at else None,
        "objects_included": objects == "embed",
        "archives": archive_list,
        "files": {n: {"sha256": files[n]["sha256"], "lines": files[n]["lines"]} for n in FILES},
    }
    manifest_bytes = canonical_json(manifest)
    shas = sorted(embedded)
    sizer = ZipSizer()
    sizer.add("manifest.json", len(manifest_bytes))
    for name in FILES:
        sizer.add(name, files[name]["bytes"])
    for sha in shas:
        sizer.add(f"objects/{sha}", embedded[sha].size)
    return JobPackagePlan(
        tenant_id=tenant_id, job_id=job_id, objects_mode=objects, archives_mode=archives,
        manifest=manifest_bytes, head_seq=head_seq, files=files, anchors=tuple(anchors),
        objects=tuple(embedded[sha] for sha in shas), page_size=page_size,
        zip_size=sizer.total(), archives=tuple(archive_list),
    )  # fmt: skip


async def _archive_refs(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, job_id: uuid.UUID
) -> list[dict[str, Any]]:
    async with tenant_tx(sessions, tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT DISTINCT archive_evidence_id FROM evidence_objects WHERE"  # noqa: S608
                    f" archive_evidence_id IS NOT NULL AND {_EVIDENCE_OF_JOB}"
                ),
                {"j": job_id},
            )
        ).all()
    return [{"archive_evidence_id": str(r.archive_evidence_id)} for r in rows]


async def _archive_limits(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, archive_ids: set[str]
) -> dict[str, Any]:
    if not archive_ids:
        return {}
    async with tenant_tx(sessions, tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT DISTINCT ON (evidence_object_id) evidence_object_id, limits"
                    " FROM slack_exports WHERE evidence_object_id = ANY(:a)"
                    " ORDER BY evidence_object_id, created_at"
                ),
                {"a": [uuid.UUID(a) for a in archive_ids]},
            )
        ).all()
    return {str(r.evidence_object_id): r.limits for r in rows}


# ------------------------------------------------------------------ pass 2: the members
async def job_package_members(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    plan: JobPackagePlan,
) -> AsyncIterator[ZipMember]:
    """The package entries in their fixed order, each checked against the plan as it streams."""
    t, j, n = plan.tenant_id, plan.job_id, plan.page_size
    bucket = settings.s3_evidence_bucket

    async def manifest() -> AsyncIterator[bytes]:
        yield plan.manifest

    yield ZipMember("manifest.json", len(plan.manifest), manifest)
    lines = {
        "events.jsonl": lambda: _event_lines(sessions, t, j, plan.head_seq, n),
        "items.jsonl": lambda: _item_lines(sessions, t, j, plan.head_seq, n),
        "evidence.jsonl": lambda: _evidence_lines(sessions, s3, bucket, t, j, n, None),
        "anchors.jsonl": lambda: _aiter(list(plan.anchors)),
    }
    for name in FILES:
        yield _jsonl_member(name, plan.files[name], lines[name])
    for obj in plan.objects:
        yield _object_member(
            s3, bucket, f"objects/{obj.sha256}", obj.key, obj.version_id, obj.sha256, obj.size
        )
