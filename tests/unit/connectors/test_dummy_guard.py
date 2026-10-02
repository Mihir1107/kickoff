"""The dummy connector shares the slack identity namespace (ADR 0004 amendment), so it is never wired
outside local/test/ci: not in the API, not in a worker (the routes refuse it too: integration tests)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from edisc_api.app import Resources
from edisc_connector_dummy.connector import DummyConnector
from edisc_connector_dummy.guard import (
    DummyNotPermittedError,
    ensure_dummy_permitted,
    source_permitted,
)
from edisc_core.settings import Environment, Settings
from edisc_worker.__main__ import build_connectors
from edisc_worker.activities import Activities

REAL = [Environment.STAGING, Environment.PRODUCTION]
DISPOSABLE = [Environment.LOCAL, Environment.TEST, Environment.CI]
NONE = cast(Any, None)  # services the guard never touches


def settings(env: Environment) -> Settings:
    return Settings(_env_file=None, env=env)  # type: ignore[call-arg]


def dummy() -> DummyConnector:
    return DummyConnector(NONE)


@pytest.mark.parametrize("env", REAL)
def test_refused_in_real_environments(env: Environment) -> None:
    assert not source_permitted(env, "dummy") and source_permitted(env, "slack_export")
    with pytest.raises(DummyNotPermittedError, match="only available when EDISC_ENV is local"):
        ensure_dummy_permitted(env, ["slack_export", "dummy"])
    ensure_dummy_permitted(env, ["slack_export"])
    with pytest.raises(DummyNotPermittedError):
        Activities(NONE, NONE, settings(env), {"dummy": dummy()})
    with pytest.raises(DummyNotPermittedError):
        Resources(settings(env), NONE, NONE, NONE, NONE, NONE, NONE, {"dummy": dummy()})
    with pytest.raises(SystemExit, match="dummy connector is only available"):
        build_connectors(NONE, ["dummy"], sessions=NONE, s3=NONE, settings=settings(env), http=NONE)
    wired = build_connectors(
        NONE, ["slack_export"], sessions=NONE, s3=NONE, settings=settings(env), http=NONE
    )
    assert set(wired) == {"slack_export"}


@pytest.mark.parametrize("env", DISPOSABLE)
def test_allowed_where_data_is_disposable(env: Environment) -> None:
    assert source_permitted(env, "dummy")
    ensure_dummy_permitted(env, ["dummy"])
    Activities(NONE, NONE, settings(env), {"dummy": dummy()})
    Resources(settings(env), NONE, NONE, NONE, NONE, NONE, NONE, {"dummy": dummy()})
    wired = build_connectors(
        NONE, ["dummy"], sessions=NONE, s3=NONE, settings=settings(env), http=NONE
    )
    assert set(wired) == {"dummy"}
