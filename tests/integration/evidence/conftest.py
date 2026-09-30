"""Evidence writer fixtures: small part sizes so multipart boundaries are cheap to exercise."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter

from ..custody.conftest import new_job

MiB = 1024 * 1024
PART = 5 * MiB  # S3 minimum part size


@pytest.fixture
def ev_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "evidence_part_size_bytes": PART,
            "evidence_single_copy_max_bytes": 6 * MiB,  # force UploadPartCopy above 6 MiB in tests
            "evidence_copy_part_size_bytes": PART,
        }
    )


@pytest.fixture
def writer(
    app_sessions: async_sessionmaker[AsyncSession], s3: S3Client, ev_settings: Settings
) -> EvidenceWriter:
    return EvidenceWriter(app_sessions, s3, ev_settings)


@dataclass(frozen=True)
class Ctx:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    matter_retention_until: datetime


async def make_ctx(sessions: async_sessionmaker[AsyncSession]) -> Ctx:
    job = await new_job(sessions)
    async with tenant_tx(sessions, job.tenant_id) as s:
        until = (
            await s.execute(
                text(
                    "SELECT m.retention_until FROM collection_jobs j JOIN matters m ON m.id = j.matter_id WHERE j.id = :j"
                ),
                {"j": job.job_id},
            )
        ).scalar_one()
    return Ctx(job.tenant_id, job.job_id, until)


@pytest.fixture
async def ctx(app_sessions: async_sessionmaker[AsyncSession]) -> Ctx:
    return await make_ctx(app_sessions)


def pattern(size: int, seed: int = 0) -> bytes:
    """Deterministic pseudo-random bytes (cheap to regenerate for hashing without keeping them)."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest() * 1024
        counter += 1
    return bytes(out[:size])


async def chunks(
    size: int, *, chunk: int = MiB, seed: int = 0, fail_after: int | None = None
) -> AsyncIterator[bytes]:
    """Stream ``size`` bytes in ``chunk``-sized pieces without holding them all; optionally explode."""
    sent = 0
    block = pattern(chunk, seed)
    while sent < size:
        n = min(chunk, size - sent)
        if fail_after is not None and sent >= fail_after:
            raise ConnectionError("source connection dropped mid-stream")
        yield block[:n] if n < chunk else block
        sent += n


def expected_sha(size: int, *, chunk: int = MiB, seed: int = 0) -> str:
    h, sent, block = hashlib.sha256(), 0, pattern(chunk, seed)
    while sent < size:
        n = min(chunk, size - sent)
        h.update(block[:n])
        sent += n
    return h.hexdigest()


async def one_shot(data: bytes) -> AsyncIterator[bytes]:
    yield data


def rand(n: int) -> bytes:
    return os.urandom(n)
