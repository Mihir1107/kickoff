"""Route registration. Every route declares the permission it needs (ADR 0013 section 3); a test
enumerates the routes and fails on any route without one."""

from __future__ import annotations

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from edisc_api.routes import admin, hierarchy, me

    app.include_router(me.router)
    app.include_router(hierarchy.router)
    app.include_router(admin.router)
