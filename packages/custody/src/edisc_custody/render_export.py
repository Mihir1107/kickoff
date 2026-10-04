"""Render package export: DB + WORM bucket -> directory in ``edisc-render-package/1`` format
(ADR 0015 §14; verified offline by ``edisc-verify``, see ``edisc_custody.render_package``).

Anchors, of the render stream and the job's seal, are always read from the bucket's object versions,
never from the database. Output files are streamed from their pinned versions into ``outputs/``, or
left out (``outputs="reference"``) for the expert to supply by hash.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.settings import Settings
from edisc_core.time import format_utc, utc_now
from edisc_custody.chain import anchor_prefix
from edisc_custody.export import _download, _HashingWriter
from edisc_custody.log import event_record_from_row
from edisc_custody.render_files import RENDER_STARTED
from edisc_custody.render_package import RENDER_FILES, RENDER_PACKAGE_FORMAT
from edisc_db.session import tenant_tx
from edisc_evidence.worm import get_bytes, list_versions


async def export_render_package(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    render_id: uuid.UUID,
    dest: Path,
    outputs: Literal["embed", "reference"] = "embed",
    page_size: int = 1000,
) -> Path:
    """Write the package of one render into ``dest`` (must not exist)."""
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=False)
    out_dir = dest / "outputs"
    if outputs == "embed":
        await asyncio.to_thread(out_dir.mkdir)
    writers = {name: _HashingWriter(dest / name) for name in RENDER_FILES}
    bucket = settings.s3_evidence_bucket

    async for version in list_versions(
        s3, bucket=bucket, prefix=anchor_prefix(str(tenant_id), str(render_id))
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

    files: list[dict[str, Any]] = []
    reference: dict[str, Any] | None = None
    async with tenant_tx(sessions, tenant_id) as session:
        render = (
            await session.execute(
                text(
                    "SELECT job_id, finished_at IS NOT NULL AS finished FROM renders WHERE id = :r"
                ),
                {"r": render_id},
            )
        ).one()
        head = (
            await session.execute(
                text("SELECT last_seq, last_hash FROM custody_chain_heads WHERE stream_id = :r"),
                {"r": render_id},
            )
        ).first()
        after = 0
        while True:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, tenant_id, stream_id, job_id, seq, event_type, actor, item_id, payload,"
                        " prev_hash, event_hash, created_at FROM custody_events WHERE stream_id = :r"
                        " AND seq > :a ORDER BY seq LIMIT :n"
                    ),
                    {"r": render_id, "a": after, "n": page_size},
                )
            ).all()
            if not rows:
                break
            for row in rows:
                ev = event_record_from_row(row)
                if row.event_type == RENDER_STARTED:
                    reference = dict(row.payload["job"])
                writers["events.jsonl"].write(
                    {"id": ev.id, "fields": ev.fields, "prev_hash": ev.prev_hash,
                     "event_hash": ev.event_hash}
                )  # fmt: skip
            after = rows[-1].seq
        last = -1
        while True:
            batch = (
                await session.execute(
                    text(
                        "SELECT rf.ord, rf.record, rf.custody_event_id, e.storage_key, e.version_id"
                        " FROM render_files rf JOIN evidence_objects e ON e.id = rf.evidence_object_id"
                        " WHERE rf.render_id = :r AND rf.ord > :a ORDER BY rf.ord LIMIT :n"
                    ),
                    {"r": render_id, "a": last, "n": page_size},
                )
            ).all()
            if not batch:
                break
            for f in batch:
                writers["files.jsonl"].write(
                    {"custody_event_id": str(f.custody_event_id), "record": dict(f.record)}
                )
                files.append(
                    {"name": f.record["name"], "key": f.storage_key, "version": f.version_id}
                )
            last = batch[-1].ord

    seal: dict[str, Any] = {"key": None, "version_id": None, "body_b64": None}
    if reference is not None:
        key, version_id = reference["seal"]["key"], reference["seal"]["version_id"]
        body = await get_bytes(s3, bucket=bucket, key=key, version_id=version_id)
        seal = {"key": key, "version_id": version_id, "body_b64": base64.b64encode(body).decode()}
    writers["job_seal.json"].write(seal)

    if outputs == "embed":
        for out in files:  # streamed from the PINNED version: an output can be large
            await _download(s3, bucket, out["key"], out["version"], out_dir / out["name"])

    manifest = {
        "format": RENDER_PACKAGE_FORMAT,
        "tenant_id": str(tenant_id),
        "render_id": str(render_id),
        "job_id": str(render.job_id),
        "finalized": bool(render.finished),
        "head": {"seq": head.last_seq, "hash": head.last_hash} if head else None,
        "exported_at": format_utc(utc_now()),
        "outputs_included": outputs == "embed",
        "files": {name: w.close() for name, w in writers.items()},
    }
    (dest / "manifest.json").write_bytes(canonical_json(manifest))
    return dest
