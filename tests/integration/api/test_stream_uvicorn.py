"""A job stream's first event arrives promptly through the REAL server (M17 plan §4, "no buffering").

The other stream tests call the ASGI app directly; here the app is served by uvicorn over TCP and
read by an HTTP client, chunk by chunk. The app is ``create_app`` itself (what ``edisc_api.main``
serves), so any middleware production adds, compression included, is in the path; the ``gzip``
case adds Starlette's ``GZipMiddleware`` in front, the compressor someone would add first.

**The bound** (``FIRST_EVENT_SECONDS`` = 5): with production stream settings and a job that does
not change, the server writes the retry line and the catch-up (one authentication, one
transaction of five statements: milliseconds here), then NOTHING until the first keepalive at
``api_stream_recheck_seconds / 2`` = 7.5 s. A layer that buffers until the next write, or until
the response ends, therefore delivers the first event after 7.5 s at the earliest, or never; 5 s
is below that and far above the honest path's cost (tens of milliseconds, under a second on a
loaded CI runner).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.middleware.gzip import GZipMiddleware

from edisc_api.app import create_app

from .conftest import Api, TenantCtx
from .sessions_flow import host_of
from .test_job_stream import new_job, settle, tuned
from .test_jobs import worker  # noqa: F401 (the collection worker fixture make_world needs)

FIRST_EVENT_SECONDS = 5.0
PRODUCTION = {"api_stream_poll_seconds": 1.0, "api_stream_recheck_seconds": 15.0}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def _served(app: Any) -> AsyncIterator[int]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off",
    ))  # fmt: skip
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 (uvicorn exposes a flag, no event)
            await asyncio.sleep(0.05)
        yield port
    finally:
        server.should_exit = True
        await serving


@pytest.mark.parametrize("stack", ["production", "gzip"])
async def test_the_first_event_arrives_within_the_bound_through_uvicorn(
    api: Api, tenant: TenantCtx, stack: str
) -> None:
    api = tuned(api, **PRODUCTION)
    job = await new_job(api, tenant)
    await settle(api, tenant, job, job.units[0])
    app: Any = create_app(api.settings, api.resources)
    if stack == "gzip":
        app = GZipMiddleware(app, minimum_size=0)
    headers = {
        "host": host_of(api, tenant),
        "authorization": f"Bearer {tenant.token(api.settings)}",
        "accept-encoding": "gzip",
    }
    async with _served(app) as port, httpx.AsyncClient(timeout=30) as client:
        started = time.monotonic()
        url = f"http://127.0.0.1:{port}/v1/jobs/{job.id}/stream"
        async with client.stream("GET", url, headers=headers) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache, no-store, no-transform"
            assert response.headers["x-accel-buffering"] == "no"
            assert "content-encoding" not in response.headers  # never compressed
            assert "content-length" not in response.headers
            text = ""
            async for chunk in response.aiter_text():
                text += chunk
                if "event: job" in text and text.endswith("\n\n"):
                    break
            arrived = time.monotonic() - started
    # the catch-up (the settled unit, then the snapshot) came before any keepalive
    blocks = [b for b in text.split("\n\n") if b]
    assert blocks[0] == "retry: 3000"
    assert [b.split("\n")[1] for b in blocks[1:3]] == ["event: units", "event: job"]
    assert arrived < FIRST_EVENT_SECONDS, f"first event after {arrived:.2f} s"
