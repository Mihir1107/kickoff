"""Custody package export: DB + WORM bucket -> directory in ``edisc-custody-package/3`` format
(ADR 0008; /3 = /2 with a deterministic manifest, ADR 0018 §27).

Format /2 added archives (ADR 0014): an export zip whose entries are evidence is either EMBEDDED
(``objects/<sha256>``, streamed, never held in memory) or REFERENCED by SHA-256 and size only, for
archives too large to ship; the expert then supplies the zip to ``edisc-verify --archive``. The
entries come from ``edisc_custody.job_export``, the same generator as the download endpoint, so a
directory export and a downloaded zip of one job and mode hold the same bytes.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.job_export import job_package_members, plan_job_package


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
    plan = await plan_job_package(
        sessions, s3, settings, tenant_id=tenant_id, job_id=job_id,
        objects="embed" if include_objects else "omit", archives=archives, page_size=page_size,
    )  # fmt: skip
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=False)
    if include_objects:
        await asyncio.to_thread((dest / "objects").mkdir)
    async for member in job_package_members(sessions, s3, settings, plan):
        path = dest / member.name
        with path.open("wb") as fh:
            async for chunk in member.chunks():
                await asyncio.to_thread(fh.write, chunk)  # an archive can be very large
    return dest
