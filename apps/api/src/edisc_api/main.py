"""ASGI entry point: ``uvicorn edisc_api.main:app`` (``make api``)."""

from __future__ import annotations

from edisc_api.app import create_app
from edisc_api.safe_errors import route_server_logs
from edisc_core.logs import configure_logging
from edisc_core.settings import Settings

configure_logging()
route_server_logs()  # no raw request line in any log (ADR 0016 amendment, D11)
app = create_app(Settings())
