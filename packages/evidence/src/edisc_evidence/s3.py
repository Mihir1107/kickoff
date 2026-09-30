"""S3 client factory. Plain S3 API only (ADR 0002): works against MinIO locally and AWS S3 in production."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings


@asynccontextmanager
async def s3_client(settings: Settings) -> AsyncIterator[S3Client]:
    config = AioConfig(
        retries={"max_attempts": 5, "mode": "standard"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_supported",
        max_pool_connections=50,
    )
    async with get_session().create_client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key.get_secret_value()
        if settings.s3_access_key
        else None,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value()
        if settings.s3_secret_key
        else None,
        config=config,
    ) as client:
        yield client
