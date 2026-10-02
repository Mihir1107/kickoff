"""slack_exports guard (migration 0018): declared columns and the locked archive never change, final
states are final, only the documented transitions happen, and the directory tables are append-only."""

from __future__ import annotations

import asyncpg
import pytest

from edisc_core.ids import new_id

from ..conftest import Connect
from .conftest import HEX, Seeded, tenant_ctx


async def _export(conn: asyncpg.Connection, t: Seeded) -> object:
    export = new_id()
    async with tenant_ctx(conn, t.tenant_id):
        client = await conn.fetchval("SELECT id FROM clients WHERE is_default")
        await conn.execute(
            "INSERT INTO slack_exports (id, tenant_id, client_id, declared_size, limits, staging_key,"
            " created_by) VALUES ($1, $2, $3, 10, '{}', 'k', 'tests')",
            export,
            t.tenant_id,
            client,
        )
    return export


async def _update(conn: asyncpg.Connection, t: Seeded, sql: str, *args: object) -> None:
    async with tenant_ctx(conn, t.tenant_id):
        await conn.execute(sql, *args)


LOCK = (
    "UPDATE slack_exports SET status = $2, sha256 = $3, size_bytes = 10, evidence_object_id = $4,"
    " version_id = 'v', locked_at = now() WHERE id = $1"
)


async def test_transitions_and_immutability(
    connect: Connect, two_tenants: tuple[Seeded, Seeded]
) -> None:
    a, _ = two_tenants
    conn = await connect("app")
    try:
        x = await _export(conn, a)
        guarded = pytest.raises(asyncpg.PostgresError, match="slack_exports")
        with guarded:
            await _update(conn, a, "UPDATE slack_exports SET declared_size = 11 WHERE id = $1", x)
        with pytest.raises(asyncpg.PostgresError, match="uploading -> validating"):
            await _update(conn, a, LOCK, x, "validating", HEX, a.evidence_id)
        await _update(conn, a, "UPDATE slack_exports SET status = 'locking' WHERE id = $1", x)
        await _update(
            conn, a, "UPDATE slack_exports SET status = 'uploading' WHERE id = $1", x
        )  # reopen
        await _update(conn, a, "UPDATE slack_exports SET status = 'locking' WHERE id = $1", x)
        await _update(conn, a, LOCK, x, "validating", HEX, a.evidence_id)
        with pytest.raises(asyncpg.PostgresError, match="locked archive is immutable"):
            await _update(
                conn, a, "UPDATE slack_exports SET sha256 = $2 WHERE id = $1", x, "cd" * 32
            )
        with pytest.raises(asyncpg.PostgresError, match="validating -> uploading"):
            await _update(conn, a, "UPDATE slack_exports SET status = 'uploading' WHERE id = $1", x)
        await _update(
            conn,
            a,
            "UPDATE slack_exports SET status = 'rejected', reject_reason = 'archive_invalid',"
            " validated_at = now() WHERE id = $1",
            x,
        )
        with pytest.raises(asyncpg.PostgresError, match="is final"):
            await _update(
                conn, a, "UPDATE slack_exports SET findings = '{\"x\": 1}' WHERE id = $1", x
            )
        y = await _export(conn, a)
        await _update(conn, a, "UPDATE slack_exports SET status = 'locking' WHERE id = $1", y)
        await _update(conn, a, LOCK, y, "validating", HEX, a.evidence_id)
        # a ready export needs its tier and its connection
        with pytest.raises(asyncpg.CheckViolationError):
            await _update(conn, a, "UPDATE slack_exports SET status = 'ready' WHERE id = $1", y)
        with pytest.raises(asyncpg.PostgresError):
            await _update(conn, a, "UPDATE export_entries SET kind = 'unknown'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await _update(conn, a, "DELETE FROM export_upload_parts")
    finally:
        await conn.close()
