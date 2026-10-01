"""Route registration. Every route declares the permission it needs (ADR 0013 section 3); a test
enumerates the routes and fails on any route without one."""

from __future__ import annotations

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from edisc_api.routes import me

    app.include_router(me.router)
