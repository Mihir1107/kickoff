"""Expire Slack export uploads that were not completed within the upload TTL (M18 §4).

``expire-export-uploads`` (maintenance, hourly): an export still ``uploading`` after
``EDISC_EXPORT_UPLOAD_TTL_DAYS`` becomes ``rejected`` with reason ``upload_expired`` (migration 0046
allows exactly this transition), which releases its quota (usage is computed from the rows,
``edisc_api.quotas``). Its multipart upload is aborted; the staged parts would expire with the
staging lifecycle anyway. One ``audit.export_upload_expired`` event per export, in the tenant
stream. An export already past ``uploading`` is never touched: a ``locking`` export is the
worker's. Every tenant is attempted; failures are raised together.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import anchor_if_due, append
from edisc_db.session import tenant_tx

ACTOR = "system:export-expiry"


@dataclass
class ExpiryResult:
    expired: list[uuid.UUID] = field(default_factory=list)


class ExportExpiryError(ExceptionGroup[Exception]):
    pass


async def expire_export_uploads(
    sweeper_sessions: async_sessionmaker[AsyncSession],
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID | None = None,
    ttl: timedelta | None = None,
) -> ExpiryResult:
    age = ttl if ttl is not None else timedelta(days=settings.export_upload_ttl_days)
    if tenant_id is not None:
        tenants = [tenant_id]
    else:
        async with sweeper_sessions() as session, session.begin():
            tenants = list(
                (await session.execute(text("SELECT id FROM tenants ORDER BY id"))).scalars()
            )
    result, failures = ExpiryResult(), list[Exception]()
    for tenant in tenants:
        try:
            result.expired += await _expire_tenant(sessions, s3, settings, tenant, age)
        except Exception as exc:  # noqa: BLE001 (collected and raised together below)
            failures.append(exc)
    if failures:
        raise ExportExpiryError("export upload expiry failed", failures)
    return result


async def _expire_tenant(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    tenant_id: uuid.UUID,
    age: timedelta,
) -> list[uuid.UUID]:
    expired: list[uuid.UUID] = []
    async with tenant_tx(sessions, tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, staging_key, upload_id, created_at FROM slack_exports"
                    " WHERE status = 'uploading' AND created_at < now() - CAST(:age AS interval)"
                    " ORDER BY id FOR NO KEY UPDATE SKIP LOCKED"
                ),
                {"age": age},
            )
        ).all()
        for row in rows:
            await s.execute(
                text(
                    "UPDATE slack_exports SET status = 'rejected', reject_reason = 'upload_expired',"
                    " updated_at = now() WHERE id = :i AND status = 'uploading'"
                ),
                {"i": row.id},
            )
            await append(
                s, tenant_id=tenant_id, stream_id=tenant_id, actor=ACTOR,
                event_type="audit.export_upload_expired",
                payload={"export_id": str(row.id), "created_at": row.created_at.isoformat(),
                         "ttl_seconds": int(age.total_seconds())},
            )  # fmt: skip
            expired.append(row.id)
    for row in rows:  # after the commit: the row no longer counts, whatever happens to the parts
        if row.upload_id:
            try:
                await asyncio.shield(
                    s3.abort_multipart_upload(
                        Bucket=settings.s3_staging_bucket,
                        Key=row.staging_key,
                        UploadId=row.upload_id,
                    )
                )
            except ClientError as exc:
                # already gone (expired by MinIO's stale-upload expiry or the lifecycle): fine
                if exc.response.get("Error", {}).get("Code") != "NoSuchUpload":
                    raise
    if expired:
        await anchor_if_due(sessions, s3, settings, tenant_id=tenant_id, stream_id=tenant_id)
    return expired
