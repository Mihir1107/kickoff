"""Range requests per 1,000 entries on a synthetic Slack export from the dummy oracle (ADR 0014 R6).

    make test-env-up
    EDISC_ENV_FILE=.env.test uv run python scripts/measure_export_reads.py --conversations 40 --days 50

Writes the export into the evidence bucket (as an upload would be locked), then measures, against the
pinned version in MinIO:

1. validation reads: end records, two central-directory passes (wrapper detection, then entries) and
   streaming the conversation metadata files;
2. a full read of every entry in local-header order with CRC/size/SHA-256 checks and overlap limits (what
   the connector does during a job), coalesced (``CoalescingSource``) and naive (one range per read).

Variants: plain, and the worst real-world shape (macOS re-zip + wrapper + data descriptors + ZIP64).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import time
from typing import Any

from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportOptions, write_export
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connector_slack_export.layout import RootDetector
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.archive import (
    ArchiveLimits,
    Source,
    iter_central_directory,
    locate_directory,
    open_entry,
)
from edisc_db.session import create_engine, create_tenant, session_factory
from edisc_evidence.archive_source import CoalescingSource, S3ObjectSource
from edisc_evidence.s3 import s3_client
from edisc_evidence.writer import EvidenceWriter

LIMITS = ArchiveLimits()


async def validation_reads(src: Source) -> int:
    directory = await locate_directory(src, LIMITS)
    detector = RootDetector()
    async for e in iter_central_directory(src, LIMITS, directory):
        detector.feed(e.name, e.is_dir)
    metadata = []
    async for e in iter_central_directory(src, LIMITS, directory):
        if e.name.endswith(("channels.json", "groups.json", "dms.json", "mpims.json")):
            metadata.append(e)
    for e in metadata:
        async for _ in open_entry(src, e, LIMITS):
            pass
    return directory.entries


async def full_read(src: Source) -> tuple[int, int]:
    directory = await locate_directory(src, LIMITS)
    entries = [e async for e in iter_central_directory(src, LIMITS, directory)]
    ordered = sorted(entries, key=lambda e: e.local_header_offset)
    produced = 0
    for i, e in enumerate(ordered):
        end = ordered[i + 1].local_header_offset if i + 1 < len(ordered) else directory.cd_offset
        async for chunk in open_entry(src, e, LIMITS, data_end_limit=end):
            produced += len(chunk)
    return len(entries), produced


async def measure(args: argparse.Namespace, name: str, opts: ExportOptions) -> dict[str, Any]:
    settings = Settings()
    spec = DatasetSpec(
        seed=3, conversations=args.conversations, days=args.days, messages_per_unit=args.messages
    )
    buf = io.BytesIO()
    manifest = write_export(Dataset(spec), buf, opts)
    data = buf.getvalue()
    engine = create_engine(settings, "app")
    sessions = session_factory(engine)
    tenant = new_id()
    await create_tenant(
        sessions,
        tenant_id=tenant,
        name="M",
        subdomain=f"m-{tenant.hex[:10]}",
        kms_key_ref="local:k",
    )
    try:
        async with s3_client(settings) as s3:
            key = f"exports/{tenant}/{new_id()}"
            await s3.put_object(Bucket=settings.s3_staging_bucket, Key=key, Body=data)
            written = await EvidenceWriter(sessions, s3, settings).lock_staged(
                tenant_id=tenant, staging_key=key, sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )  # fmt: skip
            await s3.delete_object(Bucket=settings.s3_staging_bucket, Key=key)

            def source(coalesced: bool) -> tuple[Source, S3ObjectSource]:
                inner = S3ObjectSource(
                    s3, bucket=settings.s3_evidence_bucket, key=written.storage_key,
                    version_id=written.version_id, size=len(data),
                )  # fmt: skip
                if coalesced:
                    return CoalescingSource(inner, window=settings.export_read_window_bytes), inner
                return inner, inner

            src, inner = source(True)
            started = time.perf_counter()
            entries = await validation_reads(src)
            validation = (inner.requests, time.perf_counter() - started)
            src, inner = source(True)
            started = time.perf_counter()
            n, produced = await full_read(src)
            coalesced = (inner.requests, time.perf_counter() - started)
            src, inner = source(False)
            started = time.perf_counter()
            await full_read(src)
            naive = (inner.requests, time.perf_counter() - started)
    finally:
        await engine.dispose()
    if not n == entries == manifest.entries:
        raise RuntimeError(f"entry counts disagree: {n}, {entries}, {manifest.entries}")
    per_k = 1000 / entries
    return {
        "variant": name,
        "entries": entries,
        "day_files": manifest.day_files,
        "messages": manifest.messages,
        "zip_mb": round(len(data) / 1e6, 1),
        "decompressed_mb": round(produced / 1e6, 1),
        "validation_requests": validation[0],
        "validation_per_1000": round(validation[0] * per_k, 2),
        "validation_s": round(validation[1], 2),
        "read_requests": coalesced[0],
        "read_per_1000": round(coalesced[0] * per_k, 2),
        "read_s": round(coalesced[1], 2),
        "naive_requests": naive[0],
        "naive_per_1000": round(naive[0] * per_k, 1),
        "naive_s": round(naive[1], 2),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=40)
    ap.add_argument("--days", type=int, default=50)
    ap.add_argument("--messages", type=int, default=12)
    args = ap.parse_args()
    variants = {
        "plain": ExportOptions(),
        "macos+wrapper+descriptors+zip64": ExportOptions(
            macos=True, wrapper="Acme Slack export", data_descriptors=True, force_zip64=True
        ),
    }
    for name, opts in variants.items():
        print(json.dumps(await measure(args, name, opts)))


if __name__ == "__main__":
    asyncio.run(main())
