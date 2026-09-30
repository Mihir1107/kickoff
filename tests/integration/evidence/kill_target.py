"""Subprocess used by the SIGKILL test: starts a multipart write, reports progress, then hangs until killed.

python -m tests.integration.evidence.kill_target <page|file> <tenant_id> <job_id> <matter_retention_iso>
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

from edisc_core.settings import Settings
from edisc_core.time import parse_utc
from edisc_db.session import create_engine, session_factory
from edisc_evidence.s3 import s3_client
from edisc_evidence.writer import EvidenceWriter

PART = 5 * 1024 * 1024


async def stalling_stream(seed: int) -> AsyncIterator[bytes]:
    block = (f"{seed}".encode() * PART)[:PART]
    for i in range(10):
        if i == 4:  # parts 1-2 have been uploaded (the uploader reads ahead one part)
            print("READY", flush=True)
            await asyncio.sleep(3600)  # wait here to be SIGKILLed
        yield block


async def main() -> None:
    kind, tenant, job, retention = (
        sys.argv[1],
        uuid.UUID(sys.argv[2]),
        uuid.UUID(sys.argv[3]),
        parse_utc(sys.argv[4]),
    )
    settings = Settings().model_copy(update={"evidence_part_size_bytes": PART})
    engine = create_engine(settings)
    async with s3_client(settings) as s3:
        writer = EvidenceWriter(session_factory(engine), s3, settings)
        fn = writer.write_page if kind == "page" else writer.write_file
        await fn(
            tenant_id=tenant,
            job_id=job,
            matter_retention_until=retention,
            stream=stalling_stream(99),
        )


if __name__ == "__main__":
    asyncio.run(main())
