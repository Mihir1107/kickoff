import importlib

import pytest

MODULES = [
    "edisc_core",
    "edisc_evidence",
    "edisc_custody",
    "edisc_connectors_base",
    "edisc_connector_dummy",
    "edisc_connector_slack",
    "edisc_connector_teams",
    "edisc_normalizer",
    "edisc_renderers",
    "edisc_api",
    "edisc_worker",
]


@pytest.mark.parametrize("name", MODULES)
def test_workspace_package_imports(name: str) -> None:
    assert importlib.import_module(name).__doc__
