"""No remote fetch from a preview (ADR 0018 §10, §15, D10): a day whose messages hold remote URLs,
`<img src=...>`, `<link>`, CSS `url(...)`, `@import` and `//host` forms is previewed through the real
API in a headless Chromium (Playwright, pinned), with the CSP header the API sends. A local HTTP
server listens on the port every one of those URLs names (every host name resolves to it): it must
receive ZERO requests, the browser must request nothing but the page itself, and the console must
show no CSP violation (nothing was even attempted). The HTML holds none of the URLs as an attribute
(the html5lib check is in the unit tests, `tests/unit/renderers/test_preview.py`)."""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import uvicorn
from playwright.async_api import async_playwright

from edisc_api.app import create_app

from .conftest import Api, FileHost, TenantCtx, collection_workers
from .test_export_collection import _export_connection, _job, _msg, _scope, _ts, _zip
from .test_jobs import make_world


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Recorder(BaseHTTPRequestHandler):
    seen: list[str] = []  # noqa: RUF012 (class-level log of every request the server got)

    def do_GET(self) -> None:
        self.seen.append(self.path)
        self.send_response(200)
        self.send_header("content-type", "image/png")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


async def test_a_preview_fetches_nothing_remote_in_a_real_browser(
    api: Api, tenant: TenantCtx
) -> None:
    trap = _free_port()
    hostile = (
        f"see https://evil.example:{trap}/a and http://127.0.0.1:{trap}/b"
        f' <img src="http://127.0.0.1:{trap}/img.png"> <link rel=stylesheet href=//evil.example:{trap}/s.css>'
        f" <style>@import 'http://127.0.0.1:{trap}/i.css'; body{{background:url(http://127.0.0.1:{trap}/u.png)}}</style>"
        f' <iframe src="http://127.0.0.1:{trap}/f"></iframe> <script src=//127.0.0.1:{trap}/x.js></script>'
    )
    files = {"general/2026-01-05.json": [_msg(_ts(5, 9), hostile), _msg(_ts(5, 10), "plain")]}
    async with collection_workers(api, FileHost()) as exp:
        w = await make_world(exp, tenant)
        async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
            export = await _export_connection(c, w.client, _zip(files))
            job = await _job(c, w.matter, {"connection_id": export["connection_id"],
                                           "scopes": [_scope(datetime(2026, 1, 5, tzinfo=UTC), 1)]})  # fmt: skip

        server = ThreadingHTTPServer(("127.0.0.1", trap), _Recorder)
        Thread(target=server.serve_forever, daemon=True).start()
        api_port = _free_port()
        web = uvicorn.Server(uvicorn.Config(create_app(exp.settings, exp.resources),
                                            host="127.0.0.1", port=api_port, log_level="warning"))  # fmt: skip
        serving = asyncio.create_task(web.serve())
        try:
            while not web.started:  # noqa: ASYNC110 (uvicorn exposes a flag, no event)
                await asyncio.sleep(0.05)
            host = f"{tenant.subdomain}.{exp.settings.api_base_domain}"
            url = (f"http://{host}:{api_port}/v1/jobs/{job['id']}/preview"
                   "?conversation=C1&day=2026-01-05")  # fmt: skip
            requested: list[str] = []
            console: list[str] = []
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    args=["--host-resolver-rules=MAP * 127.0.0.1"]  # every host -> the trap
                )
                context = await browser.new_context(extra_http_headers={
                    "authorization": f"Bearer {tenant.token(exp.settings)}"})  # fmt: skip
                page = await context.new_page()
                page.on("request", lambda r: requested.append(r.url))
                page.on("console", lambda m: console.append(m.text))
                response = await page.goto(url, wait_until="networkidle")
                assert response is not None and response.status == 200
                text = await page.inner_text("body")
                await browser.close()
        finally:
            web.should_exit = True
            await serving
            server.shutdown()
    assert f"http://127.0.0.1:{trap}/img.png" in text  # shown as text
    assert requested == [url], requested  # the page itself, nothing else
    assert _Recorder.seen == [], _Recorder.seen
    assert not [m for m in console if "Content Security Policy" in m], console
