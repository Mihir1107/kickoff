"""The generic credential routes exist only where the dummy connector does (ADR 0016 A7, D10).

The production-install CI job checks the same against the production install; this runs with the
unit tests."""

from __future__ import annotations

from edisc_api.app import create_app
from edisc_core.settings import Environment, Settings

GENERIC = {
    ("POST", "/v1/clients/{client_id}/connections"),
    ("POST", "/v1/connections/{connection_id}/reauth"),
}


def _routes(env: Environment) -> set[tuple[str, str]]:
    doc = create_app(Settings.model_construct(env=env)).openapi()
    return {(m.upper(), path) for path, item in doc["paths"].items() for m in item}


def test_production_and_staging_builds_have_no_generic_credential_route() -> None:
    for env in Environment:
        present = GENERIC & _routes(env)
        assert present == (GENERIC if env.is_disposable else set()), (env, present)
