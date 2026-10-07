"""Custody package export: DB + WORM bucket -> directory in ``edisc-custody-package/2`` format (ADR 0008).

Format /2 adds archives (ADR 0014): an export zip whose entries are evidence is either EMBEDDED
(``objects/<sha256>``, streamed, never held in memory) or REFERENCED by SHA-256 and size only, for
archives too large to ship; the expert then supplies the zip to ``edisc-verify --archive``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from pathlib import Path
from typing import IO, Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_core.time import format_utc, utc_now
from edisc_custody.chain import BATCH_EVENT, anchor_prefix
from edisc_custody.log import event_record_from_row
from edisc_custody.package import FILES, PACKAGE_FORMAT
from edisc_db.session import tenant_tx
from edisc_evidence.worm import get_bytes, list_versions


class _HashingWriter:
    def __init__(self, path: Path) -> None:
        self._fh: IO[bytes] = path.open("wb")
        self._sha = hashlib.sha256()
        self.lines = 0

    def write(self, obj: Any) -> None:
        line = canonical_json(obj) + b"\n"
        self._fh.write(line)
        self._sha.update(line)
        self.lines += 1

    def close(self) -> dict[str, Any]:
        self._fh.close()
        return {"sha256": self._sha.hexdigest(), "lines": self.lines}


# ------------------------------------------------------------------ export
async def export_package(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    dest: Path,
    include_objects: bool = True,
    archives: Literal["embed", "reference"] = "embed",
    page_size: int = 1000,
) -> Path:
    """Write the package for one job's custody stream into ``dest`` (must not exist)."""
    objects_dir = dest / "objects"
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=False)
    if include_objects:
        await asyncio.to_thread(objects_dir.mkdir)
    writers = {name: _HashingWriter(dest / name) for name in FILES}
    bucket = settings.s3_evidence_bucket

    async for version in list_versions(
        s3, bucket=bucket, prefix=anchor_prefix(str(tenant_id), str(job_id))
    ):
        record: dict[str, Any] = {"key": version.key, "version_id": version.version_id}
        if version.is_delete_marker:
            record["delete_marker"] = True
        else:
            body = await get_bytes(
                s3, bucket=bucket, key=version.key, version_id=version.version_id
            )
            record["body_b64"] = base64.b64encode(body).decode()
        writers["anchors.jsonl"].write(record)

    async with tenant_tx(sessions, tenant_id) as session:
        job = (
            await session.execute(
                text(
                    "SELECT finished_at IS NOT NULL AS finished FROM collection_jobs WHERE id = :j"
                ),
                {"j": job_id},
            )
        ).one()
        head = (
            await session.execute(
                text("SELECT last_seq, last_hash FROM custody_chain_heads WHERE stream_id = :j"),
                {"j": job_id},
            )
        ).first()
        after = 0
        while True:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, tenant_id, stream_id, job_id, seq, event_type, actor, item_id, payload,"
                        " prev_hash, event_hash, created_at FROM custody_events WHERE stream_id = :j AND seq > :a"
                        " ORDER BY seq LIMIT :n"
                    ),
                    {"j": job_id, "a": after, "n": page_size},
                )
            ).all()
            if not rows:
                break
            for row in rows:
                ev = event_record_from_row(row)
                writers["events.jsonl"].write(
                    {
                        "id": ev.id,
                        "fields": ev.fields,
                        "prev_hash": ev.prev_hash,
                        "event_hash": ev.event_hash,
                    }
                )
                if row.event_type == BATCH_EVENT:
                    items = await session.execute(
                        text(
                            "SELECT i.id, i.source, i.source_item_id, i.version, i.item_type, i.event_kind,"
                            " i.idempotency_key, i.content_hash, i.raw_hash, i.evidence_object_id, i.storage_key,"
                            " i.json_path, ji.custody_event_id, ji.unit_key FROM job_items ji"
                            " JOIN items i ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id"
                            " WHERE ji.custody_event_id = :e ORDER BY i.idempotency_key"
                        ),
                        {"e": row.id},
                    )
                    for it in items:
                        writers["items.jsonl"].write({k: _str(v) for k, v in it._mapping.items()})
            after = rows[-1].seq

        evidence = await session.execute(
            text(
                # the job's own objects, plus content-addressed files first stored by another job and
                # shared by dedup: those are found through this job's items, not by job_id
                "SELECT id, storage_key, kind, state, sha256, size_bytes, version_id, source_sha256,"
                " source_hash_origin, archive_evidence_id, entry_path, entry_raw_name, entry_crc32,"
                " entry_compressed_size FROM evidence_objects"
                " WHERE job_id = :j OR id IN (SELECT i.evidence_object_id FROM job_items ji"
                " JOIN items i ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id WHERE ji.job_id = :j)"
                # ... and the archives (export zips) holding any of those entries
                " OR id IN (SELECT e.archive_evidence_id FROM evidence_objects e WHERE e.job_id = :j"
                " OR e.id IN (SELECT i.evidence_object_id FROM job_items ji JOIN items i"
                " ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id WHERE ji.job_id = :j))"
                " ORDER BY storage_key"
            ),
            {"j": job_id},
        )
        evidence_rows = [_evidence_record(dict(r._mapping)) for r in evidence]
        archive_ids = {
            r["archive_evidence_id"] for r in evidence_rows if r.get("archive_evidence_id")
        }
        limits = {
            str(r.evidence_object_id): r.limits
            for r in (
                await session.execute(
                    text(
                        "SELECT DISTINCT ON (evidence_object_id) evidence_object_id, limits"
                        " FROM slack_exports WHERE evidence_object_id = ANY(:a)"
                        " ORDER BY evidence_object_id, created_at"
                    ),
                    {"a": [uuid.UUID(a) for a in archive_ids]},
                )
            ).all()
        }

    for ev_row in evidence_rows:
        # Record every OTHER version at the key: shadows are storage incidents the expert must see.
        shadows = sorted(
            [
                v.version_id
                async for v in list_versions(s3, bucket=bucket, prefix=ev_row["storage_key"])
                if v.key == ev_row["storage_key"] and v.version_id != ev_row["version_id"]
            ]
        )
        writers["evidence.jsonl"].write(
            {**{k: _str(v) for k, v in ev_row.items()}, "shadow_versions": shadows}
        )
        is_archive = str(ev_row["id"]) in archive_ids
        if is_archive and include_objects and archives == "embed":
            target = objects_dir / ev_row["sha256"]
            if not await asyncio.to_thread(target.exists):  # streamed: a zip can be very large
                await _download(s3, bucket, ev_row["storage_key"], ev_row["version_id"], target)
        elif (
            include_objects
            and not is_archive
            and ev_row["state"] == "complete"
            and ev_row["kind"] in ("page", "file")
        ):
            target = objects_dir / ev_row["sha256"]
            if not await asyncio.to_thread(target.exists):
                # always the PINNED version, never "latest" (which may be a shadow)
                data = await get_bytes(
                    s3, bucket=bucket, key=ev_row["storage_key"], version_id=ev_row["version_id"]
                )
                await asyncio.to_thread(target.write_bytes, data)

    manifest = {
        "format": PACKAGE_FORMAT,
        "tenant_id": str(tenant_id),
        "stream_id": str(job_id),
        "job_id": str(job_id),
        "finalized": bool(job.finished),
        "head": {"seq": head.last_seq, "hash": head.last_hash} if head else None,
        "exported_at": format_utc(utc_now()),
        "objects_included": include_objects,
        "archives": [
            {
                "evidence_id": str(r["id"]),
                "storage_key": r["storage_key"],
                "version_id": r["version_id"],
                "sha256": r["sha256"],
                "size_bytes": r["size_bytes"],
                "embedded": include_objects and archives == "embed",
                "limits": limits.get(str(r["id"])),
            }
            for r in evidence_rows
            if str(r["id"]) in archive_ids
        ],
        "files": {name: w.close() for name, w in writers.items()},
    }
    await asyncio.to_thread((dest / "manifest.json").write_bytes, canonical_json(manifest))
    return dest


def _evidence_record(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.pop("entry_raw_name", None)
    if row.get("kind") != "archive_entry":
        for key in ("archive_evidence_id", "entry_path", "entry_crc32", "entry_compressed_size"):
            row.pop(key, None)
    else:
        row["entry_raw_name_b64"] = base64.b64encode(bytes(raw)).decode()
    return {k: _str(v) for k, v in row.items()}


async def _download(s3: S3Client, bucket: str, key: str, version_id: str, target: Path) -> None:
    """Stream the PINNED version to a file (bounded memory), written under a temporary name."""
    partial = target.with_name(target.name + ".partial")
    resp = await s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    with partial.open("wb") as fh:
        async with resp["Body"] as body:
            async for chunk in body.iter_chunks(1 << 20):
                await asyncio.to_thread(fh.write, chunk)  # a zip can be very large
    partial.replace(target)


def _str(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value
