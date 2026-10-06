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
            "INSERT INTO work_unit_scopes (tenant_id, job_id, unit_key, scope_id) VALUES ($1, $2, $3, $4)",
            t,
            ids["job"],
            unit_key,
            ids["scope"],
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
        await conn.execute(
            "INSERT INTO job_pauses (id, tenant_id, job_id, connection_id, reason) VALUES ($1, $2, $3, $4, 'r')",
            new_id(),
            t,
            ids["job"],
            ids["conn"],
        )
        await conn.execute(
            "INSERT INTO alerts (id, tenant_id, kind, message) VALUES ($1, $2, 'k', 'm')",
            new_id(),
            t,
        )
        # M13 hierarchy and principals (migration 0015); the default client was created by the trigger
        await conn.execute(
            "INSERT INTO workspaces (id, tenant_id, matter_id, name) VALUES ($1, $2, $3, 'W')",
            new_id(),
            t,
            ids["matter"],
        )
        await conn.execute(
            "INSERT INTO tenant_idps (id, tenant_id, issuer, audience, jwks_url) VALUES ($1, $2, 'https://idp', 'edisc', 'https://idp/jwks')",
            new_id(),
            t,
        )
        principal, group = new_id(), new_id()
        await conn.execute(
            "INSERT INTO principals (id, tenant_id, kind, issuer, subject, display_name) VALUES ($1, $2, 'user', 'https://idp', $3, 'U')",
            principal,
            t,
            f"sub-{principal}",
        )
        await conn.execute(
            "INSERT INTO groups (id, tenant_id, name) VALUES ($1, $2, 'G')", group, t
        )
        await conn.execute(
            "INSERT INTO group_members (id, tenant_id, group_id, principal_id) VALUES ($1, $2, $3, $4)",
            new_id(),
            t,
            group,
            principal,
        )
        await conn.execute(
            "INSERT INTO role_assignments (id, tenant_id, principal_id, role, scope_type, created_by)"
            " VALUES ($1, $2, $3, 'tenant_admin', 'tenant', 'seed')",
            new_id(),
            t,
            principal,
        )
        await conn.execute(
            "INSERT INTO api_idempotency (tenant_id, key, principal_id, request_hash, job_id) VALUES ($1, 'k1', $2, 'h', $3)",
            t,
            principal,
            ids["job"],
        )
        # Slack exports (migration 0018)
        client = await conn.fetchval("SELECT id FROM clients WHERE is_default")
        export = new_id()
        await conn.execute(
            "INSERT INTO slack_exports (id, tenant_id, client_id, declared_size, limits, staging_key,"
            " created_by) VALUES ($1, $2, $3, 10, '{}', $4, 'seed')",
            export,
            t,
            client,
            f"exports/{t}/{export}",
        )
        await conn.execute(
            "INSERT INTO export_upload_parts (tenant_id, export_id, part_number, size_bytes, sha256, etag)"
            " VALUES ($1, $2, 1, 10, $3, 'e')",
            t,
            export,
            HEX,
        )
        await conn.execute(
            "INSERT INTO export_entries (tenant_id, export_id, idx, name, folded_name, kind, method,"
            " crc32, compressed_size, uncompressed_size, local_header_offset, raw_name, name_encoding)"
            " VALUES ($1, $2, 0, 'users.json', 'users.json', 'metadata', 0, 0, 2, 2, 0, 'users.json',"
            " 'ascii')",
            t,
            export,
        )
        await conn.execute(
            "INSERT INTO export_conversations (tenant_id, export_id, conversation_id, kind, folder,"
            " metadata_entry) VALUES ($1, $2, 'C1', 'channel', 'general', 'channels.json')",
            t,
            export,
        )
        await conn.execute(
            "INSERT INTO export_day_files (tenant_id, export_id, entry_idx, elements) VALUES ($1, $2, 0, 1)",
            t,
            export,
        )
        await conn.execute(
            "INSERT INTO export_threads (tenant_id, export_id, entry_idx, element_idx, conversation_id,"
            " thread_ts, ts) VALUES ($1, $2, 0, 0, 'C1', '1.000001', '1.000001')",
            t,
            export,
        )
        # renders (migration 0026)
        render = new_id()
        await conn.execute(
            "INSERT INTO renders (id, tenant_id, job_id, matter_id, options, options_hash,"
            " renderer_version, unicode_version, tzdata_version, requested_by)"
            " VALUES ($1, $2, $3, $4, '{}', $5, '1', '1', '1', 'seed')",
            render,
            t,
            ids["job"],
            ids["matter"],
            HEX,
        )
        await conn.execute(
            "INSERT INTO render_files (tenant_id, render_id, ord, name, evidence_object_id, version_id,"
            " sha256, size_bytes, record, custody_event_id) VALUES ($1, $2, 0, 'f.rsmf', $3, 'v', $4,"
            " 1, '{}', $5)",
            t,
            render,
            ids["ev"],
            HEX,
            ids["event"],
        )
        await conn.execute(
            "INSERT INTO render_natives (tenant_id, render_id, ord, sha256, size_bytes, storage_key,"
            " version_id, file_ords, evidence_object_id, custody_event_id) VALUES ($1, $2, 0, $4, 1, $6,"
            " 'v', '{0}', $3, $5)",
            t,
            render,
            ids["ev"],
            HEX,
            ids["event"],
            f"t/{t}/productions/{render}/natives/sha256/{HEX}",
        )
        await conn.execute(
            "INSERT INTO production_episodes (id, tenant_id, render_id, subject_id, kind)"
            " VALUES ($1, $2, $3, $3, 'unroutable')",
            new_id(),
            t,
            render,
        )
        # reports (migration 0031)
        report = new_id()
        await conn.execute(
            "INSERT INTO reports (id, tenant_id, job_id, matter_id, renderer_version,"
            " unicode_version, toolchain_id, paper, requested_by)"
            " VALUES ($1, $2, $3, $4, '1', '1', 'none', 'letter', 'seed')",
            report,
            t,
            ids["job"],
            ids["matter"],
        )
        await conn.execute(
            "INSERT INTO report_files (tenant_id, report_id, ord, name, media_type,"
            " evidence_object_id, version_id, sha256, size_bytes, rows)"
            " VALUES ($1, $2, 0, 'report.json', 'application/json', $3, 'v', $4, 1, NULL)",
            t,
            report,
            ids["ev"],
            HEX,
        )
        await conn.execute(
            "INSERT INTO retention_gaps (id, tenant_id, evidence_object_id, owner_type, owner_id,"
            " unprotected_from, unprotected_until, outcome) VALUES ($1, $2, $3, 'matter', $4, now(),"
            " now(), 'relocked')",
            new_id(),
            t,
            ids["ev"],
            ids["matter"],
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
    "job_pauses",
    "alerts",
    "clients",
    "workspaces",
    "tenant_idps",
    "principals",
    "groups",
    "group_members",
    "role_assignments",
    "api_idempotency",
    "work_unit_scopes",
    "slack_exports",
    "export_upload_parts",
    "export_entries",
    "export_conversations",
    "retention_gaps",
    "export_day_files",
    "export_threads",
    "renders",
    "render_files",
    "render_natives",
    "production_episodes",
    "reports",
    "report_files",
]
ALL_TABLES = ["tenants", *TENANT_TABLES]
