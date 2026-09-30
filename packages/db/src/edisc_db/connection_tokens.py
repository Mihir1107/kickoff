"""Encrypted connection tokens: store, load, atomic refresh, key rotation (rewrap).

Guarantees
- Access and refresh tokens are each sealed with :class:`edisc_core.envelope.SecretBox` under the
  tenant's KEK, bound to (tenant_id, connection_id, purpose).
- Every write replaces BOTH blobs, the expiry, the key reference and ``token_version`` in ONE UPDATE, so
  a reader sees either the old token set or the new one, never a mix.
- Refresh holds the connection row lock (``SELECT ... FOR UPDATE``) from reading the current refresh
  token until the new pair is committed. Concurrent refreshers queue on the lock and, with
  ``min_valid_for``, reuse a pair another worker just obtained instead of spending the refresh token
  twice (rotating refresh tokens are single-use at most providers).
- If the provider call or the write fails, the transaction rolls back and the row is untouched.
- Plaintext tokens exist only as ``SecretStr``, are registered with the log redactor on decrypt, and are
  never passed to Temporal, logged or included in exceptions.

Custody events for connection lifecycle (created, validated, refreshed, rotated) are appended by the
calling service (API/worker), which owns the custody stream.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_core.envelope import SecretBox, SecretContext, SecretPurpose
from edisc_core.time import ensure_utc, utc_now
from edisc_db.session import tenant_tx


class StaleTokenVersionError(RuntimeError):
    """Someone else wrote the tokens since they were read (optimistic concurrency)."""


class NoTokensError(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenSet:
    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_at: datetime | None
    version: int = 0

    def expires_within(self, delta: timedelta) -> bool:
        return self.expires_at is not None and ensure_utc(self.expires_at) <= utc_now() + delta


def _ctx(tenant_id: uuid.UUID, connection_id: uuid.UUID, purpose: SecretPurpose) -> SecretContext:
    return SecretContext(tenant_id=tenant_id, connection_id=connection_id, purpose=purpose)


async def _kek(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    key: str = (
        await session.execute(
            text("SELECT kms_key_ref FROM tenants WHERE id = :t"), {"t": tenant_id}
        )
    ).scalar_one()
    return key


async def _write(
    session: AsyncSession,
    box: SecretBox,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    tokens: TokenSet,
    expected_version: int,
) -> int:
    key_id = await _kek(session, tenant_id)
    access = await box.seal(
        tokens.access_token,
        key_id=key_id,
        context=_ctx(tenant_id, connection_id, SecretPurpose.CONNECTION_ACCESS_TOKEN),
    )
    refresh = (
        await box.seal(
            tokens.refresh_token,
            key_id=key_id,
            context=_ctx(tenant_id, connection_id, SecretPurpose.CONNECTION_REFRESH_TOKEN),
        )
        if tokens.refresh_token is not None
        else None
    )
    new_version: int | None = (
        await session.execute(
            text(
                "UPDATE connections SET encrypted_access_token = :a, encrypted_refresh_token = :r,"
                " token_expires_at = :e, token_key_id = :k, token_key_version = :kv,"
                " token_version = token_version + 1, token_updated_at = now(), updated_at = now()"
                " WHERE id = :c AND token_version = :expected RETURNING token_version"
            ),
            {
                "a": access.blob,
                "r": refresh.blob if refresh else None,
                "e": tokens.expires_at,
                "k": access.key_id,
                "kv": access.key_version,
                "c": connection_id,
                "expected": expected_version,
            },
        )
    ).scalar_one_or_none()
    if new_version is None:
        raise StaleTokenVersionError(
            f"connection {connection_id}: tokens changed since version {expected_version}"
        )
    return new_version


_READ = (
    "SELECT encrypted_access_token, encrypted_refresh_token, token_expires_at, token_version"
    " FROM connections WHERE id = :c"
)
_READ_LOCKED = _READ + " FOR UPDATE"


async def _read(
    session: AsyncSession,
    box: SecretBox,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    lock: bool,
) -> TokenSet:
    row = (
        await session.execute(
            text(_READ_LOCKED if lock else _READ),
            {"c": connection_id},
        )
    ).one_or_none()
    if row is None or row.encrypted_access_token is None:
        raise NoTokensError(f"connection {connection_id} has no stored tokens")
    access = await box.open(
        row.encrypted_access_token,
        context=_ctx(tenant_id, connection_id, SecretPurpose.CONNECTION_ACCESS_TOKEN),
    )
    refresh = (
        await box.open(
            row.encrypted_refresh_token,
            context=_ctx(tenant_id, connection_id, SecretPurpose.CONNECTION_REFRESH_TOKEN),
        )
        if row.encrypted_refresh_token is not None
        else None
    )
    return TokenSet(access, refresh, row.token_expires_at, row.token_version)


# ------------------------------------------------------------------ public API
async def store_tokens(
    sessions: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    tokens: TokenSet,
    expected_version: int,
) -> int:
    """Initial store (after OAuth / install) or replacement. Returns the new token_version."""
    async with tenant_tx(sessions, tenant_id) as session:
        return await _write(
            session,
            box,
            tenant_id=tenant_id,
            connection_id=connection_id,
            tokens=tokens,
            expected_version=expected_version,
        )


async def load_tokens(
    sessions: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> TokenSet:
    async with tenant_tx(sessions, tenant_id) as session:
        return await _read(
            session, box, tenant_id=tenant_id, connection_id=connection_id, lock=False
        )


Refresher = Callable[[TokenSet], Awaitable[TokenSet]]


async def refresh_tokens(
    sessions: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    refresher: Refresher,
    min_valid_for: timedelta | None = None,
) -> TokenSet:
    """Atomically exchange the refresh token for a new pair.

    ``refresher`` calls the provider with the current tokens and returns the new set. It runs while the
    row is locked; if it raises, nothing is written. With ``min_valid_for``, a pair that is still valid
    for that long (e.g. just refreshed by another worker) is returned without calling the provider.
    """
    async with tenant_tx(sessions, tenant_id) as session:
        current = await _read(
            session, box, tenant_id=tenant_id, connection_id=connection_id, lock=True
        )
        if min_valid_for is not None and not current.expires_within(min_valid_for):
            return current
        fresh = await refresher(current)
        version = await _write(
            session,
            box,
            tenant_id=tenant_id,
            connection_id=connection_id,
            tokens=fresh,
            expected_version=current.version,
        )
        return TokenSet(fresh.access_token, fresh.refresh_token, fresh.expires_at, version)


async def rewrap_tenant_tokens(
    sessions: async_sessionmaker[AsyncSession],
    box: SecretBox,
    *,
    tenant_id: uuid.UUID,
    destination_key_id: str | None = None,
) -> int:
    """Key rotation: re-wrap every connection token of the tenant under the current KEK version (via KMS
    ReEncrypt; no plaintext handled). Each connection is rewritten atomically. Returns rows rewrapped."""
    async with tenant_tx(sessions, tenant_id) as session:
        ids: list[uuid.UUID] = list(
            (
                await session.execute(
                    text("SELECT id FROM connections WHERE encrypted_access_token IS NOT NULL")
                )
            )
            .scalars()
            .all()
        )
    count = 0
    for connection_id in ids:
        async with tenant_tx(sessions, tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT encrypted_access_token, encrypted_refresh_token, token_version"
                        " FROM connections WHERE id = :c FOR UPDATE"
                    ),
                    {"c": connection_id},
                )
            ).one()
            access = await box.rewrap(
                row.encrypted_access_token,
                context=_ctx(tenant_id, connection_id, SecretPurpose.CONNECTION_ACCESS_TOKEN),
                destination_key_id=destination_key_id,
            )
            refresh = (
                await box.rewrap(
                    row.encrypted_refresh_token,
                    context=_ctx(tenant_id, connection_id, SecretPurpose.CONNECTION_REFRESH_TOKEN),
                    destination_key_id=destination_key_id,
                )
                if row.encrypted_refresh_token is not None
                else None
            )
            await session.execute(
                text(
                    "UPDATE connections SET encrypted_access_token = :a, encrypted_refresh_token = :r,"
                    " token_key_id = :k, token_key_version = :kv, token_version = token_version + 1,"
                    " token_updated_at = now() WHERE id = :c AND token_version = :v"
                ),
                {
                    "a": access.blob,
                    "r": refresh.blob if refresh else None,
                    "k": access.key_id,
                    "kv": access.key_version,
                    "c": connection_id,
                    "v": row.token_version,
                },
            )
        count += 1
    return count
