"""Drive one request at the ASGI level and read the database at the moment the FIRST body byte is
sent: proves that a content read is audited and its audit anchored before any byte leaves."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from edisc_api.app import create_app
from edisc_custody.log import anchor_if_due
from edisc_db.session import tenant_tx

from .conftest import Api, TenantCtx


@dataclass
class FirstByte:
    status: int
    newest: Any  # the newest audit event (seq, event_type, payload) when the first byte was sent
    anchored: int  # the audit stream's last anchored seq at that moment
    body_bytes: int  # the whole body, once the response ended


async def _state(api: Api, t: TenantCtx) -> tuple[Any, int]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        newest = (
            await s.execute(
                text(
                    "SELECT seq, event_type, payload FROM custody_events WHERE stream_id = :t"
                    " ORDER BY seq DESC LIMIT 1"
                ),
                {"t": t.tenant_id},
            )
        ).one()
        anchored = (
            await s.execute(
                text("SELECT last_anchored_seq FROM custody_chain_heads WHERE stream_id = :t"),
                {"t": t.tenant_id},
            )
        ).scalar_one()
    return newest, int(anchored)


async def first_byte(api: Api, t: TenantCtx, path: str, query: str = "") -> FirstByte:
    # anchor the audit stream first, so the read lands mid-interval: an "anchor if due" after it
    # writes nothing, and only a forced anchor covers it
    await anchor_if_due(
        api.sessions, api.s3, api.settings, tenant_id=t.tenant_id, stream_id=t.tenant_id,
        force=True,
    )  # fmt: skip
    app = create_app(api.settings, api.resources)
    host = f"{t.subdomain}.{api.settings.api_base_domain}"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": query.encode(), "client": ("127.0.0.1", 50000), "server": (host, 80),
        "headers": [(b"host", host.encode()),
                    (b"authorization", f"Bearer {t.token(api.settings)}".encode())],
    }  # fmt: skip
    seen: dict[str, Any] = {"bytes": 0}
    requested = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if requested.is_set():  # the client stays connected until the response ends
            await asyncio.Event().wait()
        requested.set()
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]
        elif message.get("body"):
            if "newest" not in seen:
                seen["newest"], seen["anchored"] = await _state(api, t)
            seen["bytes"] += len(message["body"])

    await app(scope, receive, send)
    assert "newest" in seen, f"no body was sent (status {seen.get('status')})"
    return FirstByte(seen["status"], seen["newest"], seen["anchored"], seen["bytes"])
