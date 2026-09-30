"""Custody package export: DB + WORM bucket -> directory in ``edisc-custody-package/1`` format (ADR 0008)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from pathlib import Path
from typing import IO, Any

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
                "SELECT id, storage_key, kind, state, sha256, size_bytes FROM evidence_objects"
                " WHERE job_id = :j ORDER BY storage_key"
            ),
            {"j": job_id},
        )
        evidence_rows = [dict(r._mapping) for r in evidence]

    for ev_row in evidence_rows:
        writers["evidence.jsonl"].write({k: _str(v) for k, v in ev_row.items()})
        if include_objects and ev_row["state"] == "complete" and ev_row["kind"] in ("page", "file"):
            target = objects_dir / ev_row["sha256"]
            if not await asyncio.to_thread(target.exists):
                data = await get_bytes(s3, bucket=bucket, key=ev_row["storage_key"])
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
        "files": {name: w.close() for name, w in writers.items()},
    }
    (dest / "manifest.json").write_bytes(canonical_json(manifest))
    return dest


def _str(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value
