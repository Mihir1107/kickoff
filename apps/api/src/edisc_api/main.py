"""ASGI entry point: ``uvicorn edisc_api.main:app`` (``make api``)."""

from __future__ import annotations

from edisc_api.app import create_app
from edisc_core.logs import configure_logging
from edisc_core.settings import Settings

configure_logging()
app = create_app(Settings())
