"""Closing is reversible only by a full reopen (migration 0021): closed_at/closed_by return to NULL
together; who closed and when cannot be rewritten while closed. Enforced for direct SQL too."""

from __future__ import annotations

import asyncpg
import pytest

from ..conftest import Connect
from .conftest import Seeded, tenant_ctx


async def test_close_reopen_and_no_rewriting(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    conn = await connect("app")
    try:

        async def run(sql: str) -> None:
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute(sql, a.matter_id)

        await run("UPDATE matters SET closed_at = now(), closed_by = 't' WHERE id = $1")
        for sql in (
            "UPDATE matters SET closed_by = 'other' WHERE id = $1",
            "UPDATE matters SET closed_at = now() - interval '1 day' WHERE id = $1",
        ):
            with pytest.raises(asyncpg.PostgresError, match="reopen before closing again"):
                await run(sql)
        with pytest.raises(asyncpg.CheckViolationError):  # half a reopen
            await run("UPDATE matters SET closed_at = NULL WHERE id = $1")
        await run("UPDATE matters SET closed_at = NULL, closed_by = NULL WHERE id = $1")  # reopen
        await run("UPDATE matters SET closed_at = now(), closed_by = 't2' WHERE id = $1")  # again
        await run("UPDATE matters SET closed_at = NULL, closed_by = NULL WHERE id = $1")
        with pytest.raises(asyncpg.PostgresError, match="default flag"):
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute("UPDATE clients SET is_default = false WHERE is_default")
    finally:
        await conn.close()
