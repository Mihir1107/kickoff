"""ADR 0014 R6: entries are read from the pinned object with few, large, coalesced range requests."""

from __future__ import annotations

import io
import json
import random
import zipfile

from types_aiobotocore_s3 import S3Client

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.archive import ArchiveLimits, scan
from edisc_evidence.archive_source import CoalescingSource, S3ObjectSource

ENTRIES = 5000


def _export_like_zip() -> bytes:
    rng = random.Random(7)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(ENTRIES):
            msgs = [
                {"type": "message", "user": f"U{rng.randrange(50)}", "ts": f"{1767571200 + i * 60 + j}.000100",
                 "text": " ".join(rng.choice(["lorem", "ipsum", "dolor", "sit", "amet", str(rng.random())]) for _ in range(30))}
                for j in range(rng.randrange(20, 60))
            ]  # fmt: skip
            zf.writestr(
                f"channel{i % 40}/2026-{1 + i // 1000:02d}-{1 + i % 28:02d}-{i}.json",
                json.dumps(msgs),
            )
    return buf.getvalue()


async def test_sequential_coalesced_reads_cost_a_few_requests_per_thousand_entries(
    s3: S3Client, settings: Settings
) -> None:
    data = _export_like_zip()
    key = f"tests/archive-source/{new_id()}.zip"
    put = await s3.put_object(Bucket=settings.s3_staging_bucket, Key=key, Body=data)
    version = put.get("VersionId") or "null"
    limits = ArchiveLimits(read_chunk=1 << 20)

    plain = S3ObjectSource(
        s3, bucket=settings.s3_staging_bucket, key=key, version_id=version, size=len(data)
    )
    window = 8 << 20
    coalesced = CoalescingSource(
        S3ObjectSource(
            s3, bucket=settings.s3_staging_bucket, key=key, version_id=version, size=len(data)
        ),
        window=window,
    )
    entries = await scan(coalesced, limits)
    assert len(entries) == ENTRIES
    per_thousand = 1000 * coalesced.requests / ENTRIES
    # ~ one request per window of archive, plus the end records and the directory
    assert coalesced.requests <= len(data) // window + 6, coalesced.requests
    # the naive reader, measured on the first 200 entries (header, name, data: ~3 requests each)
    from edisc_custody.archive import open_entry

    for e in sorted(entries, key=lambda e: e.local_header_offset)[:200]:
        async for _ in open_entry(plain, e, limits):
            pass
    print(
        f"archive {len(data)} bytes, {ENTRIES} entries: coalesced {coalesced.requests} requests "
        f"({per_thousand:.2f} per 1,000 entries) vs uncoalesced {1000 * plain.requests / 200:.0f} per 1,000"
    )
    assert plain.requests >= 2 * 200  # the naive reader: at least header + data per entry
    await s3.delete_object(Bucket=settings.s3_staging_bucket, Key=key)
