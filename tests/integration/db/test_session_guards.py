"""The guard triggers of ``sessions`` and ``login_attempts`` (migration 0036, ADR 0016; M17 step 2):
identity columns immutable, a revoked session final, activity never moving back, an attempt consumed
once with one final outcome, the idle expiry never past the absolute one. As the app role, on a
database migrated from scratch HERE (so a change to the migration itself is what is tested)."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Awaitable, Callable

import asyncpg
import pytest

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import upgrade

from .conftest import Connect, Seeded, seed_tenant, tenant_ctx

SCRATCH_DB = "edisc_session_guards"
Scratch = Callable[[], Awaitable[asyncpg.Connection]]


@pytest.fixture(scope="module")
async def scratch(connect: Connect, settings: Settings) -> tuple[Scratch, Seeded]:
    su = await connect("superuser", db="postgres")
    try:
        await su.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    finally:
        await su.close()
    await bootstrap(settings, db=SCRATCH_DB)
    await asyncio.to_thread(upgrade, SCRATCH_DB)

    async def app() -> asyncpg.Connection:
        return await connect("app", db=SCRATCH_DB)

    conn = await app()
    try:
        seeded = await seed_tenant(conn)
    finally:
        await conn.close()
    return app, seeded


async def _fixture(conn: asyncpg.Connection, tenant: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A fresh session and login attempt in ``tenant`` (the seed's principal and IdP)."""
    principal = await conn.fetchval("SELECT id FROM principals LIMIT 1")
    idp = await conn.fetchval("SELECT id FROM tenant_idps LIMIT 1")
    session, attempt = new_id(), new_id()
    await conn.execute(
        "INSERT INTO sessions (id, tenant_id, principal_id, idp_id, id_hash, authenticated_at,"
        " idle_expires_at, absolute_expires_at) VALUES ($1, $2, $3, $4, $5, now(),"
        " now() + interval '30 minutes', now() + interval '12 hours')",
        session, tenant, principal, idp, secrets.token_bytes(32),
    )  # fmt: skip
    await conn.execute(
        "INSERT INTO login_attempts (id, tenant_id, idp_id, state_hash, handle_hash, key_id, nonce,"
        " return_to, reauth, expires_at) VALUES ($1, $2, $3, $4, $5, 'dev', 'n', '/', false,"
        " now() + interval '10 minutes')",
        attempt, tenant, idp, secrets.token_bytes(32), secrets.token_bytes(32),
    )  # fmt: skip
    return session, attempt


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE sessions SET principal_id = gen_random_uuid() WHERE id = $1",
        "UPDATE sessions SET id_hash = sha256(id_hash) WHERE id = $1",
        "UPDATE sessions SET authenticated_at = now() - interval '1 day' WHERE id = $1",
        "UPDATE sessions SET absolute_expires_at = absolute_expires_at + interval '1 day' WHERE id = $1",
        "UPDATE sessions SET idp_groups = '{admins}' WHERE id = $1",
        "UPDATE sessions SET last_seen_at = last_seen_at - interval '1 minute' WHERE id = $1",
        "UPDATE sessions SET idle_expires_at = idle_expires_at - interval '1 minute' WHERE id = $1",
    ],
)
async def test_a_session_keeps_its_identity_and_its_clock_only_moves_forward(
    scratch: tuple[Scratch, Seeded], statement: str
) -> None:
    app, a = scratch
    conn = await app()
    try:
        async with tenant_ctx(conn, a.tenant_id):
            session, _ = await _fixture(conn, a.tenant_id)
        with pytest.raises(asyncpg.PostgresError) as exc:
            async with tenant_ctx(conn, a.tenant_id):
                await conn.execute(statement, session)
        assert exc.value.sqlstate in {"EA002", "23503"}  # the guard (or the FK for a new principal)
    finally:
        await conn.close()


async def test_a_revoked_session_is_final(scratch: tuple[Scratch, Seeded]) -> None:
    app, a = scratch
    conn = await app()
    try:
        async with tenant_ctx(conn, a.tenant_id):
            session, _ = await _fixture(conn, a.tenant_id)
            await conn.execute(
                "UPDATE sessions SET revoked_at = now(), revoked_by = 'x', revoke_reason = 'logout'"
                " WHERE id = $1",
                session,
            )
        for statement in (
            "UPDATE sessions SET revoked_at = NULL, revoked_by = NULL, revoke_reason = NULL WHERE id = $1",
            "UPDATE sessions SET revoke_reason = 'admin' WHERE id = $1",
            "UPDATE sessions SET last_seen_at = now() + interval '1 minute' WHERE id = $1",
        ):
            with pytest.raises(asyncpg.PostgresError) as exc:
                async with tenant_ctx(conn, a.tenant_id):
                    await conn.execute(statement, session)
            assert exc.value.sqlstate == "EA002", statement
    finally:
        await conn.close()


async def test_a_session_cannot_be_revoked_without_a_reason_or_with_one_not_on_the_list(
    scratch: tuple[Scratch, Seeded],
) -> None:
    app, a = scratch
    conn = await app()
    try:
        async with tenant_ctx(conn, a.tenant_id):
            session, _ = await _fixture(conn, a.tenant_id)
        for assignment in ("revoked_at = now()", "revoked_at = now(), revoked_by = 'x', revoke_reason = 'bored'",
                           "idle_expires_at = absolute_expires_at + interval '1 second'"):  # fmt: skip
            with pytest.raises(asyncpg.CheckViolationError):
                async with tenant_ctx(conn, a.tenant_id):
                    await conn.execute(f"UPDATE sessions SET {assignment} WHERE id = $1", session)
    finally:
        await conn.close()


async def test_a_login_attempt_is_consumed_once_and_its_outcome_is_final(
    scratch: tuple[Scratch, Seeded],
) -> None:
    app, a = scratch
    conn = await app()
    try:
        async with tenant_ctx(conn, a.tenant_id):
            _, attempt = await _fixture(conn, a.tenant_id)
            with pytest.raises(asyncpg.CheckViolationError):  # an outcome needs the consume first
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE login_attempts SET outcome = 'invalid' WHERE id = $1", attempt
                    )
            await conn.execute(
                "UPDATE login_attempts SET consumed_at = now() WHERE id = $1", attempt
            )
            await conn.execute(
                "UPDATE login_attempts SET outcome = 'invalid' WHERE id = $1", attempt
            )
        for statement in (
            "UPDATE login_attempts SET consumed_at = now() + interval '1 second' WHERE id = $1",
            "UPDATE login_attempts SET consumed_at = NULL WHERE id = $1",
            "UPDATE login_attempts SET outcome = 'denied' WHERE id = $1",
            "UPDATE login_attempts SET return_to = '/other' WHERE id = $1",
            "UPDATE login_attempts SET expires_at = now() + interval '1 day' WHERE id = $1",
            "UPDATE login_attempts SET nonce = 'other' WHERE id = $1",
        ):
            with pytest.raises(asyncpg.PostgresError) as exc:
                async with tenant_ctx(conn, a.tenant_id):
                    await conn.execute(statement, attempt)
            assert exc.value.sqlstate == "EA002", statement
    finally:
        await conn.close()


async def test_a_completed_attempt_names_its_session_and_only_then(
    scratch: tuple[Scratch, Seeded],
) -> None:
    app, a = scratch
    conn = await app()
    try:
        async with tenant_ctx(conn, a.tenant_id):
            session, attempt = await _fixture(conn, a.tenant_id)
            await conn.execute(
                "UPDATE login_attempts SET consumed_at = now() WHERE id = $1", attempt
            )
        for assignment, args in (
            ("outcome = 'completed'", ()),
            ("outcome = 'invalid', session_id = $2", (session,)),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                async with tenant_ctx(conn, a.tenant_id):
                    await conn.execute(
                        f"UPDATE login_attempts SET {assignment} WHERE id = $1", attempt, *args
                    )
        async with tenant_ctx(conn, a.tenant_id):
            await conn.execute(
                "UPDATE login_attempts SET outcome = 'completed', session_id = $2 WHERE id = $1",
                attempt, session,
            )  # fmt: skip
    finally:
        await conn.close()
