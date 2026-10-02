"""Closed matters and clients stay closed (migration 0019), even for direct SQL through the app role."""

from __future__ import annotations

import asyncpg
import pytest

from ..conftest import Connect
from .conftest import Seeded, tenant_ctx


async def test_closing_is_irreversible(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    conn = await connect("app")
    try:
        async with tenant_ctx(conn, a.tenant_id):
            await conn.execute(
                "UPDATE matters SET closed_at = now(), closed_by = 't' WHERE id = $1", a.matter_id
            )
        for sql in (
            "UPDATE matters SET closed_at = NULL, closed_by = NULL WHERE id = $1",
            "UPDATE matters SET closed_by = 'other' WHERE id = $1",
        ):
            with pytest.raises(asyncpg.PostgresError, match="stays closed"):
                async with tenant_ctx(conn, a.tenant_id):
                    await conn.execute(sql, a.matter_id)
        with pytest.raises(asyncpg.CheckViolationError):
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute("UPDATE clients SET closed_at = now() WHERE is_default")
        with pytest.raises(asyncpg.PostgresError, match="default flag"):
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute("UPDATE clients SET is_default = false WHERE is_default")
    finally:
        await conn.close()
