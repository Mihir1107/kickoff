"""Integration fixtures. These talk to the real compose services (`make up`); nothing is mocked."""

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    values.update({k: v for k, v in os.environ.items() if k.startswith(("EDISC_", "TEMPORAL_"))})
    return values


@pytest.fixture(scope="session")
def env() -> Mapping[str, str]:
    return _load_env()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(pytest.mark.integration)
