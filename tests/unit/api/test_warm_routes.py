"""FastAPI builds each router's routes lazily, on the event loop, at the first request that matches;
the app builds them at startup in a thread instead (ADR 0015 §24)."""

from __future__ import annotations

from fastapi.routing import _IncludedRouter
from fastapi.testclient import TestClient

from edisc_api.app import Resources, create_app, warm_routes
from edisc_core.settings import Settings


def _cold(app: object) -> list[object]:
    routers = [r for r in app.router.routes if isinstance(r, _IncludedRouter)]  # type: ignore[attr-defined]
    return [r for r in routers if r._effective_candidates_version is None]


def test_warm_routes_builds_every_router() -> None:
    app = create_app(Settings(), resources=object())  # type: ignore[arg-type]
    assert _cold(app), "nothing to warm: FastAPI changed how it builds routes"
    assert warm_routes(app) > 20
    assert _cold(app) == []


def test_the_app_warms_its_routes_at_startup() -> None:
    app = create_app(Settings(), resources=object())  # type: ignore[arg-type]
    with TestClient(app):
        assert _cold(app) == []
    _ = Resources
