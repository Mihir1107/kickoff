"""Integration fixtures. These talk to the real compose services (`make up`); nothing is mocked."""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Literal

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import upgrade
from edisc_db.session import create_engine, session_factory
from edisc_evidence.s3 import s3_client

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = REPO_ROOT / os.environ.get("EDISC_ENV_FILE", ".env")
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] == "'":
                    value = value[1:-1].replace("'\\''", "'")
                values[key.strip()] = value
    values.update({k: v for k, v in os.environ.items() if k.startswith(("EDISC_", "TEMPORAL_"))})
    return values


@pytest.fixture(scope="session")
def env() -> Mapping[str, str]:
    return _load_env()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


Role = Literal["app", "owner", "superuser", "sweeper"]
Connect = Callable[..., Awaitable[asyncpg.Connection]]


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
async def migrated(settings: Settings) -> None:
    await bootstrap(settings)
    await asyncio.to_thread(upgrade)


@pytest.fixture(scope="session")
def connect(settings: Settings, migrated: None) -> Connect:
    async def _connect(role: Role = "app", db: str | None = None) -> asyncpg.Connection:
        return await asyncpg.connect(
            settings.pg_dsn(role, db=db), server_settings={"search_path": "edisc,pg_temp"}
        )

    return _connect


@pytest.fixture(scope="session")
async def app_sessions(
    settings: Settings, migrated: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(settings, "app", pool_size=40)
    yield session_factory(engine)
    await engine.dispose()


@pytest.fixture(scope="session")
async def sweeper_sessions(
    settings: Settings, migrated: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(settings, "sweeper", pool_size=2)
    yield session_factory(engine)
    await engine.dispose()


@pytest.fixture(scope="session")
async def s3(settings: Settings) -> AsyncIterator[S3Client]:
    async with s3_client(settings) as client:
        yield client
