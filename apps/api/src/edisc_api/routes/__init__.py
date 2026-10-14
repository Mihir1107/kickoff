"""Route registration. Every route declares the permission it needs (ADR 0013 section 3); a test
enumerates the routes and fails on any route without one."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI


def register(app: FastAPI) -> list[Any]:
    """Include every router; returns them (``safe_errors.install`` reads their routes)."""
    from edisc_api.routes import (
        admin,
        auth,
        connections,
        exports,
        hierarchy,
        job_package,
        jobs,
        me,
        preview,
        renders,
        reports,
    )

    routers = [
        me.router, auth.router, hierarchy.router, admin.router, connections.router, jobs.router, exports.router,
        renders.router, reports.router, job_package.router, preview.router,
    ]  # fmt: skip
    for router in routers:
        app.include_router(router)
    return routers
