"""Route registration. Every route declares the permission it needs (ADR 0013 section 3); a test
enumerates the routes and fails on any route without one."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from edisc_core.settings import Settings


def register(app: FastAPI, settings: Settings) -> list[Any]:
    """Include every router; returns them (``safe_errors.install`` reads their routes). The
    generic credential routes exist only where the dummy connector does (ADR 0016 A7, D10)."""
    from edisc_api.routes import (
        admin,
        auth,
        connections,
        exports,
        hierarchy,
        installs,
        job_package,
        jobs,
        me,
        preview,
        renders,
        reports,
        wellknown,
    )
    from edisc_connector_dummy.guard import dummy_permitted

    routers = [
        me.router, auth.router, hierarchy.router, admin.router, connections.router, jobs.router, exports.router,
        renders.router, reports.router, job_package.router, preview.router, wellknown.router,
        installs.router,
    ]  # fmt: skip
    if dummy_permitted(settings.env):
        routers.append(connections.credential_router)
    for router in routers:
        app.include_router(router)
    return routers
