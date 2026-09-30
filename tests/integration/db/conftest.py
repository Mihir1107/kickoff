"""DB seeding helpers. Shared fixtures (settings, migrated, connect) live in tests/integration/conftest.py."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
import pytest

from edisc_core.ids import new_id

from ..conftest import Connect


@asynccontextmanager
async def tenant_ctx(conn: asyncpg.Connection, tenant_id: uuid.UUID | None) -> AsyncIterator[None]:
    async with conn.transaction():
        if tenant_id is not None:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        yield


@dataclass(frozen=True)
class Seeded:
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    connection_id: uuid.UUID
    custodian_id: uuid.UUID
    job_id: uuid.UUID
    unit_key: str
    evidence_id: uuid.UUID
    event_id: uuid.UUID
    item_id: uuid.UUID


HEX = "ab" * 32


async def seed_tenant(conn: asyncpg.Connection) -> Seeded:
    """Create one row in every tenant-scoped table, through the APP role and the normal grants."""
    t = new_id()
    await conn.fetchval(
        "SELECT create_tenant($1, $2, $3, $4)", t, "Acme", f"t-{secrets.token_hex(6)}", f"local:{t}"
    )
    ids = {
        k: new_id()
        for k in ("matter", "conn", "cust", "ident", "job", "scope", "ev", "event", "item")
    }
    unit_key = "C1/2026-01-01"
    async with tenant_ctx(conn, t):
        await conn.execute(
            "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES ($1, $2, 'M', now() + interval '1 day')",
            ids["matter"],
            t,
        )
        await conn.execute(
            "INSERT INTO connections (id, tenant_id, source, external_org_id, status) VALUES ($1, $2, 'dummy', 'org', 'active')",
            ids["conn"],
            t,
        )
        await conn.execute(
            "INSERT INTO custodians (id, tenant_id, display_name) VALUES ($1, $2, 'Alice')",
            ids["cust"],
            t,
        )
        await conn.execute(
            "INSERT INTO custodian_identities (id, tenant_id, custodian_id, source, external_user_id)"
            " VALUES ($1, $2, $3, 'dummy', 'U1')",
            ids["ident"],
            t,
            ids["cust"],
        )
        await conn.execute(
            "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status, connector_version, requested_by)"
            " VALUES ($1, $2, $3, $4, 'running', '0.1.0', 'tester')",
            ids["job"],
            t,
            ids["matter"],
            ids["conn"],
        )
        await conn.execute(
            "INSERT INTO collection_scopes (id, tenant_id, job_id, scope_type, external_id, date_from, date_to)"
            " VALUES ($1, $2, $3, 'channel', 'C1', now() - interval '1 day', now())",
            ids["scope"],
            t,
            ids["job"],
        )
        await conn.execute(
            "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day) VALUES ($1, $2, $3, 'C1', '2026-01-01')",
            t,
            ids["job"],
            unit_key,
        )
        await conn.execute(
            "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until, source_sha256,"
            " source_hash_origin) VALUES ($1, $2, $3, $4, 'page', now() + interval '1 day', $5, 'collection')",
            ids["ev"],
            t,
            ids["job"],
            f"test/{ids['ev']}",
            HEX,
        )
        await conn.execute(
            "UPDATE evidence_objects SET state = 'complete', sha256 = $2, size_bytes = 10, version_id = 'v-test',"
            " completed_at = now() WHERE id = $1",
            ids["ev"],
            HEX,
        )
        await conn.execute(
            "INSERT INTO custody_chain_heads (stream_id, tenant_id, last_seq, last_hash) VALUES ($1, $2, 1, $3)",
            ids["job"],
            t,
            HEX,
        )
        await conn.execute(
            "INSERT INTO custody_events (id, tenant_id, stream_id, job_id, seq, event_type, actor, payload, prev_hash, event_hash, created_at)"
            " VALUES ($1, $2, $3, $3, 1, 'job_started', 'tester', '{}', $4, $4, now())",
            ids["event"],
            t,
            ids["job"],
            HEX,
        )
        await conn.execute(
            "INSERT INTO items (id, tenant_id, job_id, source, source_item_id, version, item_type, content_hash, raw_hash,"
            " evidence_object_id, storage_key, json_path, connector_version, normalizer_version, idempotency_key)"
            " VALUES ($1, $2, $3, 'dummy', 'C1/1', 1, 'message', $4, $4, $5, $6, '$.messages[0]', '0.1.0', '0.1.0', $7)",
            ids["item"],
            t,
            ids["job"],
            HEX,
            ids["ev"],
            f"test/{ids['ev']}",
            secrets.token_hex(32),
        )
        await conn.execute(
            "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id) VALUES ($1, $2, $3, $4, $5)",
            t,
            ids["job"],
            ids["item"],
            unit_key,
            ids["event"],
        )
        await conn.execute(
            "INSERT INTO item_derivations (tenant_id, item_id, normalizer_version, derived, derived_hash)"
            " VALUES ($1, $2, '0.0.0-test', '{}', $3)",
            t,
            ids["item"],
            HEX,
        )
        await conn.execute(
            "INSERT INTO token_refresh_journal (id, tenant_id, connection_id, based_on_version,"
            " encrypted_access_token, token_key_id, token_key_version) VALUES ($1, $2, $3, 0, 'x', 'k', '1')",
            new_id(),
            t,
            ids["conn"],
        )
    return Seeded(
        t,
        ids["matter"],
        ids["conn"],
        ids["cust"],
        ids["job"],
        unit_key,
        ids["ev"],
        ids["event"],
        ids["item"],
    )


@pytest.fixture(scope="session")
async def two_tenants(connect: Connect) -> tuple[Seeded, Seeded]:
    conn = await connect("app")
    try:
        return await seed_tenant(conn), await seed_tenant(conn)
    finally:
        await conn.close()


TENANT_TABLES = [
    "matters",
    "connections",
    "custodians",
    "custodian_identities",
    "collection_jobs",
    "collection_scopes",
    "work_units",
    "evidence_objects",
    "items",
    "job_items",
    "custody_events",
    "custody_chain_heads",
    "item_derivations",
    "token_refresh_journal",
]
ALL_TABLES = ["tenants", *TENANT_TABLES]
