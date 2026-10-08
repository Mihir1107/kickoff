"""Integration fixtures. These talk to the real compose services (`make up`); nothing is mocked."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import asyncpg
import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.settings import Settings
from edisc_db.bootstrap import bootstrap
from edisc_db.migrate import upgrade
from edisc_db.session import create_engine, session_factory
from edisc_evidence.s3 import s3_client
from tests.integration import artifacts

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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(pytest.mark.integration)
    # CI sharding (ADR 0015 §24): keep only this job's balanced shard, deselect the rest. Every
    # collected test lands in exactly one shard (scripts/ci_shard.assign), so the shards together
    # run the whole suite once. Off (EDISC_CI_SHARDS unset or <= 1) outside the sharded CI job.
    shards = int(os.environ.get("EDISC_CI_SHARDS") or "0")
    if shards <= 1:
        return
    from scripts.ci_shard import assign, load_durations

    shard = int(os.environ["EDISC_CI_SHARD"])
    where = assign([i.nodeid for i in items], shards, load_durations())
    keep = [i for i in items if where[i.nodeid] == shard]
    config.hook.pytest_deselected(items=[i for i in items if where[i.nodeid] != shard])
    items[:] = keep


# ------------------------------------------------------------------ failure artifacts (CI)
_FAILED = pytest.StashKey[dict[str, artifacts.Failed]]()
_STARTED = pytest.StashKey[datetime]()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, Any, None]:
    item.stash[_STARTED] = artifacts.now()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Any, None]:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    if not report.failed or not os.environ.get("EDISC_TEST_ARTIFACTS_DIR"):
        return
    failed = item.config.stash.setdefault(_FAILED, {})
    entry = failed.setdefault(
        item.nodeid, artifacts.Failed(item.nodeid, item.stash[_STARTED], artifacts.now())
    )
    entry.stop, entry.phases = artifacts.now(), [*entry.phases, report.when]
    tmp = getattr(item, "funcargs", {}).get("tmp_path")
    if isinstance(tmp, Path):
        entry.tmp_path = tmp


def pytest_sessionfinish(session: pytest.Session) -> None:
    root = os.environ.get("EDISC_TEST_ARTIFACTS_DIR")
    failed = session.config.stash.get(_FAILED, {})
    if not root or not failed:
        return
    try:
        notes = artifacts.collect(list(failed.values()), Path(root))
    except Exception as exc:  # the run has failed already: say loudly why artifacts are missing
        print(f"\nFAILURE ARTIFACTS NOT COLLECTED: {exc!r}", file=sys.stderr)
        return
    print(f"\nfailure artifacts in {root}:\n" + "\n".join(notes), file=sys.stderr)


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


@pytest.fixture(scope="session")
async def temporal(settings: Settings) -> Client:
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


@pytest.fixture(scope="session")
async def limiter(settings: Settings) -> AsyncIterator[RateLimiter]:
    client = aioredis.from_url(settings.redis_url)
    # short wait chunks so the heartbeat tests can use short heartbeat timeouts
    yield RateLimiter(client, settings.rate_limits, wait_chunk_seconds=0.3)
    await client.aclose()
