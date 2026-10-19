"""New Redis connections never resolve redis-py's version on the event loop (`redis_driver`):
redis-py read installed-package metadata for every new connection, which blocked the API's loop
for 301 ms when its pool grew under a burst (CI run 39132450451)."""

from __future__ import annotations

from importlib import metadata

import pytest

from edisc_api.app import redis_client
from edisc_connectors_base.redis_driver import DRIVER_INFO
from edisc_core.settings import Environment, Settings


def test_api_pool_connections_carry_the_resolved_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[str] = []

    def version(name: str) -> str:
        looked_up.append(name)
        return "0"

    monkeypatch.setattr(metadata, "version", version)
    pool = redis_client(Settings(env=Environment.TEST)).connection_pool
    connections = [pool.make_connection() for _ in range(3)]
    assert looked_up == []  # nothing resolved per connection
    assert all(c.driver_info is DRIVER_INFO for c in connections)
    assert DRIVER_INFO.lib_version == metadata.distribution("redis").version
