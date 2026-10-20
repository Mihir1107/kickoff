"""S3 client factory. Plain S3 API only (ADR 0002): works against MinIO locally and AWS S3 in production."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings

Identity = Literal["app", "hold", "destroy"]


def credentials(settings: Settings, identity: Identity) -> tuple[str | None, str | None]:
    """The access key pair of one storage identity (M18 §3). ``app`` is the API and every
    collection/export/render/report worker; ``hold`` only the holds worker; ``destroy`` only the
    destruction worker. A missing hold/destroy pair is refused, never replaced by the app's."""
    key, secret = {
        "app": (settings.s3_access_key, settings.s3_secret_key),
        "hold": (settings.s3_hold_access_key, settings.s3_hold_secret_key),
        "destroy": (settings.s3_destroy_access_key, settings.s3_destroy_secret_key),
    }[identity]
    if identity != "app" and (key is None or secret is None):
        raise RuntimeError(f"the {identity} storage identity is not configured")
    return (
        key.get_secret_value() if key else None,
        secret.get_secret_value() if secret else None,
    )


@asynccontextmanager
async def s3_client(settings: Settings, identity: Identity = "app") -> AsyncIterator[S3Client]:
    config = AioConfig(
        retries={"max_attempts": 5, "mode": "standard"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_supported",
        max_pool_connections=50,
    )
    key, secret = credentials(settings, identity)
    async with get_session().create_client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=config,
    ) as client:
        yield client
