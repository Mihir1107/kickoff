"""M7: connection tokens are envelope-encrypted, context-bound, rotatable, atomically refreshed, never logged."""

from __future__ import annotations

import asyncio
import io
import logging
import secrets
import sys
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_core.envelope import DecryptionError, SecretBox, SecretContext, SecretPurpose
from edisc_core.ids import new_id
from edisc_core.kms import LocalKmsClient
from edisc_core.logs import configure_logging, get_logger
from edisc_core.redaction import clear_registered_secrets
from edisc_core.settings import Settings
from edisc_core.time import utc_now
from edisc_db.connection_tokens import (
    StaleTokenVersionError,
    TokenSet,
    load_tokens,
    refresh_tokens,
    rewrap_tenant_tokens,
    store_tokens,
)
from edisc_db.session import create_tenant, tenant_tx

from ..conftest import Connect

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def kms(settings: Settings, tmp_path: Path) -> LocalKmsClient:
    return LocalKmsClient(settings, directory=tmp_path / "kms")


@pytest.fixture
def box(kms: LocalKmsClient) -> SecretBox:
    return SecretBox(kms)


async def new_tenant(sessions: Sessions, kms: LocalKmsClient) -> uuid.UUID:
    tenant_id = new_id()
    key = f"local:tenant/{tenant_id}"
    kms.create_key(key)
    await create_tenant(
        sessions,
        tenant_id=tenant_id,
        name="T",
        subdomain=f"k-{secrets.token_hex(6)}",
        kms_key_ref=key,
    )
    return tenant_id


async def new_connection(sessions: Sessions, tenant_id: uuid.UUID) -> uuid.UUID:
    connection_id = new_id()
    async with tenant_tx(sessions, tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO connections (id, tenant_id, source, external_org_id, status) VALUES (:c, :t, 'dummy', 'org', 'active')"
            ),
            {"c": connection_id, "t": tenant_id},
        )
    return connection_id


def tokens(gen: str, *, expires_in: timedelta = timedelta(hours=1)) -> TokenSet:
    return TokenSet(
        # opaque (not pattern-shaped) so only value registration can redact them
        SecretStr(f"atk-{gen}-{secrets.token_hex(16)}"),
        SecretStr(f"rtk-{gen}-{secrets.token_hex(16)}"),
        utc_now() + expires_in,
    )


async def test_roundtrip_and_nothing_readable_at_rest(
    app_sessions: Sessions, box: SecretBox, kms: LocalKmsClient, connect: Connect
) -> None:
    t = await new_tenant(app_sessions, kms)
    c = await new_connection(app_sessions, t)
    original = tokens("g1")
    assert (
        await store_tokens(
            app_sessions, box, tenant_id=t, connection_id=c, tokens=original, expected_version=0
        )
        == 1
    )
    loaded = await load_tokens(app_sessions, box, tenant_id=t, connection_id=c)
    assert loaded.access_token.get_secret_value() == original.access_token.get_secret_value()
    assert loaded.refresh_token is not None
    assert original.refresh_token is not None
    assert loaded.refresh_token.get_secret_value() == original.refresh_token.get_secret_value()
    su = await connect("superuser")
    try:
        row = await su.fetchrow("SELECT * FROM connections WHERE id = $1", c)
    finally:
        await su.close()
    assert row is not None
    raw = bytes(row["encrypted_access_token"]) + bytes(row["encrypted_refresh_token"])
    assert original.access_token.get_secret_value().encode() not in raw
    assert original.refresh_token.get_secret_value().encode() not in raw
    assert (row["token_key_id"], row["token_key_version"], row["token_version"]) == (
        f"local:tenant/{t}",
        "1",
        1,
    )


@pytest.mark.parametrize(
    "target", ["same-tenant-other-connection", "other-tenant", "swap-access-and-refresh"]
)
async def test_blob_copied_to_another_row_or_column_fails(
    app_sessions: Sessions, box: SecretBox, kms: LocalKmsClient, connect: Connect, target: str
) -> None:
    t1 = await new_tenant(app_sessions, kms)
    a, b = await new_connection(app_sessions, t1), await new_connection(app_sessions, t1)
    t2 = await new_tenant(app_sessions, kms)
    other = await new_connection(app_sessions, t2)
    for tenant, conn in ((t1, a), (t1, b), (t2, other)):
        await store_tokens(
            app_sessions,
            box,
            tenant_id=tenant,
            connection_id=conn,
            tokens=tokens("g1"),
            expected_version=0,
        )

    su = await connect("superuser")  # a DBA/attacker moving ciphertext around
    try:
        if target == "swap-access-and-refresh":
            await su.execute(
                "UPDATE connections SET encrypted_access_token = encrypted_refresh_token WHERE id = $1",
                a,
            )
            victim_tenant, victim = t1, a
        else:
            victim_tenant, victim = (
                (t1, b) if target == "same-tenant-other-connection" else (t2, other)
            )
            await su.execute(
                "UPDATE connections SET encrypted_access_token = (SELECT encrypted_access_token FROM connections WHERE id = $1),"
                " encrypted_refresh_token = (SELECT encrypted_refresh_token FROM connections WHERE id = $1) WHERE id = $2",
                a,
                victim,
            )
    finally:
        await su.close()
    with pytest.raises(DecryptionError):
        await load_tokens(app_sessions, box, tenant_id=victim_tenant, connection_id=victim)


async def test_key_rotation_end_to_end(
    app_sessions: Sessions, box: SecretBox, kms: LocalKmsClient, connect: Connect
) -> None:
    t = await new_tenant(app_sessions, kms)
    key = f"local:tenant/{t}"
    conns = [await new_connection(app_sessions, t) for _ in range(3)]
    originals = {c: tokens(f"c{i}") for i, c in enumerate(conns)}
    for c, tok in originals.items():
        await store_tokens(
            app_sessions, box, tenant_id=t, connection_id=c, tokens=tok, expected_version=0
        )

    assert kms.rotate(key) == "2"
    assert await rewrap_tenant_tokens(app_sessions, box, tenant_id=t) == 3
    kms.disable_version(key, "1")  # retire the old key material

    su = await connect("superuser")
    try:
        rows = await su.fetch(
            "SELECT id, token_key_version, token_version FROM connections WHERE tenant_id = $1", t
        )
    finally:
        await su.close()
    assert {(r["token_key_version"], r["token_version"]) for r in rows} == {("2", 2)}
    for c, tok in originals.items():
        loaded = await load_tokens(app_sessions, box, tenant_id=t, connection_id=c)
        assert loaded.access_token.get_secret_value() == tok.access_token.get_secret_value()

    # negative control: without the rewrap, retiring the old version makes tokens unreadable
    t2 = await new_tenant(app_sessions, kms)
    c2 = await new_connection(app_sessions, t2)
    await store_tokens(
        app_sessions, box, tenant_id=t2, connection_id=c2, tokens=tokens("x"), expected_version=0
    )
    kms.rotate(f"local:tenant/{t2}")
    kms.disable_version(f"local:tenant/{t2}", "1")
    with pytest.raises(DecryptionError):
        await load_tokens(app_sessions, box, tenant_id=t2, connection_id=c2)


async def test_concurrent_refreshers_spend_the_refresh_token_once(
    app_sessions: Sessions, box: SecretBox, kms: LocalKmsClient
) -> None:
    t = await new_tenant(app_sessions, kms)
    c = await new_connection(app_sessions, t)
    await store_tokens(
        app_sessions,
        box,
        tenant_id=t,
        connection_id=c,
        tokens=tokens("g1", expires_in=timedelta(seconds=30)),
        expected_version=0,
    )
    calls: list[str] = []

    async def provider(current: TokenSet) -> TokenSet:
        assert current.refresh_token is not None
        calls.append(current.refresh_token.get_secret_value())
        await asyncio.sleep(0.2)  # network
        return tokens("g2")

    results = await asyncio.gather(
        *(
            refresh_tokens(
                app_sessions,
                box,
                tenant_id=t,
                connection_id=c,
                refresher=provider,
                min_valid_for=timedelta(minutes=5),
            )
            for _ in range(8)
        )
    )
    assert len(calls) == 1  # one provider exchange; the other 7 reused the fresh pair
    assert len({r.access_token.get_secret_value() for r in results}) == 1
    assert {r.version for r in results} == {2}


async def test_failed_refresh_leaves_the_row_untouched(
    app_sessions: Sessions, box: SecretBox, kms: LocalKmsClient
) -> None:
    t = await new_tenant(app_sessions, kms)
    c = await new_connection(app_sessions, t)
    first = tokens("g1")
    await store_tokens(
        app_sessions, box, tenant_id=t, connection_id=c, tokens=first, expected_version=0
    )

    async def broken(_: TokenSet) -> TokenSet:
        raise ConnectionError("provider 503")

    with pytest.raises(ConnectionError):
        await refresh_tokens(app_sessions, box, tenant_id=t, connection_id=c, refresher=broken)
    after = await load_tokens(app_sessions, box, tenant_id=t, connection_id=c)
    assert after.version == 1
    assert after.access_token.get_secret_value() == first.access_token.get_secret_value()


async def test_readers_never_see_a_mixed_token_set(
    app_sessions: Sessions, box: SecretBox, kms: LocalKmsClient
) -> None:
    t = await new_tenant(app_sessions, kms)
    c = await new_connection(app_sessions, t)
    await store_tokens(
        app_sessions, box, tenant_id=t, connection_id=c, tokens=tokens("g1"), expected_version=0
    )
    started = asyncio.Event()

    async def slow(_: TokenSet) -> TokenSet:
        started.set()
        await asyncio.sleep(0.5)
        return tokens("g2")

    refresh = asyncio.create_task(
        refresh_tokens(app_sessions, box, tenant_id=t, connection_id=c, refresher=slow)
    )
    await started.wait()
    during = await load_tokens(
        app_sessions, box, tenant_id=t, connection_id=c
    )  # while the row is locked
    await refresh
    after = await load_tokens(app_sessions, box, tenant_id=t, connection_id=c)

    def generation(ts: TokenSet) -> set[str]:
        assert ts.refresh_token is not None
        return {
            ts.access_token.get_secret_value().split("-")[1],
            ts.refresh_token.get_secret_value().split("-")[1],
        }

    assert generation(during) == {"g1"}
    assert generation(after) == {"g2"}


async def test_stale_writer_is_rejected(
    app_sessions: Sessions, box: SecretBox, kms: LocalKmsClient
) -> None:
    t = await new_tenant(app_sessions, kms)
    c = await new_connection(app_sessions, t)
    await store_tokens(
        app_sessions, box, tenant_id=t, connection_id=c, tokens=tokens("g1"), expected_version=0
    )
    with pytest.raises(StaleTokenVersionError):
        await store_tokens(
            app_sessions, box, tenant_id=t, connection_id=c, tokens=tokens("g2"), expected_version=0
        )


# ------------------------------------------------------------------ secrets never in logs (full path)
@pytest.fixture
def log_sink() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    old_hook = sys.excepthook
    clear_registered_secrets()
    configure_logging("DEBUG", stream=stream)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO
    )  # logs every statement AND its parameters
    yield stream
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger().handlers.clear()
    sys.excepthook = old_hook
    clear_registered_secrets()


async def test_no_token_material_in_logs_across_the_whole_lifecycle(
    app_sessions: Sessions, box: SecretBox, kms: LocalKmsClient, log_sink: io.StringIO
) -> None:
    log = get_logger("connection-service")
    provider_log = logging.getLogger("thirdparty.oauth")  # a careless provider library
    t = await new_tenant(app_sessions, kms)
    c = await new_connection(app_sessions, t)
    first, second = tokens("g1"), tokens("g2")
    canaries = [
        first.access_token.get_secret_value(),
        first.refresh_token.get_secret_value(),  # type: ignore[union-attr]
        second.access_token.get_secret_value(),
        second.refresh_token.get_secret_value(),  # type: ignore[union-attr]
    ]

    await store_tokens(
        app_sessions, box, tenant_id=t, connection_id=c, tokens=first, expected_version=0
    )
    current = await load_tokens(app_sessions, box, tenant_id=t, connection_id=c)
    log.info("loaded tokens", tokens=current, conn=str(c))

    async def provider(tok: TokenSet) -> TokenSet:
        assert tok.refresh_token is not None
        provider_log.debug(
            "POST /oauth.v2.access refresh_token=%s", tok.refresh_token.get_secret_value()
        )
        return second

    await refresh_tokens(app_sessions, box, tenant_id=t, connection_id=c, refresher=provider)
    provider_log.info("issued %s", second.access_token.get_secret_value())
    kms.rotate(f"local:tenant/{t}")
    await rewrap_tenant_tokens(app_sessions, box, tenant_id=t)
    try:
        bogus = b'{"v":1,"alg":"AES-256-GCM","key_id":"x","key_version":"1","wrapped_dek":"AA==","nonce":"AA==","ct":"AA=="}'
        await box.open(
            bogus,
            context=SecretContext(
                tenant_id=t, connection_id=c, purpose=SecretPurpose.CONNECTION_ACCESS_TOKEN
            ),
        )
    except DecryptionError:
        log.exception("decrypt failed", refresh_token=current.refresh_token)
    try:
        raise RuntimeError(f"provider rejected {first.refresh_token.get_secret_value()}")  # type: ignore[union-attr]
    except RuntimeError:
        log.exception("refresh failed")

    output = log_sink.getvalue()
    assert "UPDATE connections" in output  # SQL logging really was on, with parameters
    assert "[REDACTED]" in output
    for canary in canaries:
        assert canary not in output, "token material leaked into logs"
