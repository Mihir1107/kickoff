"""Drive one content download and end its session mid-transfer (M17 step 5b, plan §1.5).

:func:`cut_download` signs in through the real flow (a cookie session) or uses a bearer principal,
starts the download at the ASGI level, HOLDS the server's second body write (a client that has
read the first piece and not yet the rest), ends the session (revocation in the database, or
deactivation of a bearer principal), waits one re-check interval, and releases: the server must cut
at that boundary. :func:`cut_through_uvicorn` does the same over a real uvicorn server and HTTP
client (the pause sits between uvicorn and the app), so what the CLIENT sees is proven too.

Settings: re-check 1 s (interval 0.5 s) and 1 KiB pieces, so every route's test object has several
boundaries; ``test_render_natives.py`` also runs a steady reader at the production 15 s bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import socket
import time
import uuid
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn
from sqlalchemy import text

from edisc_api.app import create_app
from edisc_api.downloads import DownloadCut
from edisc_db.session import tenant_tx

from .conftest import Api, TenantCtx, add_principal
from .oidc import Browser
from .sessions_flow import enable_login, host_of, sign_in
from .test_job_stream import tuned

CUT = {"api_stream_recheck_seconds": 1.0, "api_download_chunk_bytes": 1024}


@dataclass
class Cut:
    status: int
    headers: dict[str, str]
    received: bytes
    error: BaseException | None
    after_revocation: float  # seconds from the session's end to the cut
    audit: Any  # the download_aborted event (seq, event_type, payload)
    anchored: int  # the audit stream's last anchored seq after the cut
    read: Any  # the read's audit event

    @property
    def declared(self) -> int:
        return int(self.headers["content-length"])

    def is_zip(self) -> bool:
        try:
            zipfile.ZipFile(io.BytesIO(self.received)).testzip()
        except zipfile.BadZipFile:
            return False
        return True


async def _events(api: Api, t: TenantCtx, *kinds: str) -> list[Any]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return list((await s.execute(text(
            "SELECT seq, event_type, payload FROM custody_events WHERE stream_id = :t"
            " AND event_type = ANY(:k) ORDER BY seq"), {"t": t.tenant_id, "k": list(kinds)},
        )).all())  # fmt: skip


async def _anchored(api: Api, t: TenantCtx) -> int:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        return int((await s.execute(text(
            "SELECT last_anchored_seq FROM custody_chain_heads WHERE stream_id = :t"),
            {"t": t.tenant_id})).scalar_one())  # fmt: skip


@dataclass
class _Holder:
    """Who downloads, and how their session ends."""

    headers: dict[str, str]
    end: Callable[[], Awaitable[None]]


async def _holder(api: Api, t: TenantCtx, how: str) -> _Holder:
    host = host_of(api, t)
    if how == "revoke":  # a cookie session, revoked in the database
        await enable_login(api, t)
        browser = Browser()
        assert (await sign_in(api, t, browser)).status_code == 200
        cookie = browser.cookie_header(host) or ""

        async def revoke() -> None:
            async with tenant_tx(api.sessions, t.tenant_id) as s:
                await s.execute(text("UPDATE sessions SET revoked_at = now(), revoked_by = 'tests',"
                                     " revoke_reason = 'admin' WHERE revoked_at IS NULL"))  # fmt: skip

        return _Holder({"cookie": cookie, "sec-fetch-site": "same-origin"}, revoke)
    assert how == "deactivate"  # a bearer principal, deactivated
    pid, subject = await add_principal(api, t, roles=[("tenant_admin", "tenant", None)])

    async def deactivate() -> None:
        async with tenant_tx(api.sessions, t.tenant_id) as s:
            await s.execute(text("UPDATE principals SET active = false WHERE id = :p"), {"p": pid})

    return _Holder({"authorization": f"Bearer {t.token(api.settings, subject=subject)}"},
                   deactivate)  # fmt: skip


def _scope(host: str, path: str, query: str, headers: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": query.encode(), "client": ("127.0.0.1", 50000), "server": (host, 80),
        "headers": [(b"host", host.encode()),
                    *((k.encode(), v.encode()) for k, v in headers.items())],
    }  # fmt: skip


@dataclass
class _Pause:
    """Holds the app's second body write until released (a client that read one piece)."""

    first: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def wrap(self, app: Any) -> Any:
        async def paused(scope: Any, receive: Any, send: Any) -> None:
            async def holding(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.body" and message.get("body"):
                    if self.first.is_set():
                        await self.release.wait()
                    await send(message)
                    self.first.set()
                    return
                await send(message)

            await app(scope, receive, holding)

        return paused


async def cut_download(
    api: Api, t: TenantCtx, path: str, query: str = "", *, how: str = "revoke"
) -> Cut:
    api = tuned(api, **CUT)
    holder = await _holder(api, t, how)
    host = host_of(api, t)
    pause = _Pause()
    app = pause.wrap(create_app(api.settings, api.resources))
    got: dict[str, Any] = {"body": bytearray()}
    requested = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if requested.is_set():  # the client stays connected
            await asyncio.Event().wait()
        requested.set()
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            got["status"] = message["status"]
            got["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            got["body"] += message.get("body", b"")

    task = asyncio.create_task(app(_scope(host, path, query, holder.headers), receive, send))
    await asyncio.wait_for(pause.first.wait(), 60)
    await holder.end()
    ended = time.monotonic()
    await asyncio.sleep(api.resources.watch.interval)  # the next boundary is past the interval
    pause.release.set()
    error: BaseException | None = None
    try:
        await asyncio.wait_for(task, 30)
    except Exception as exc:
        error = exc
    cut_at = time.monotonic()
    aborted = await _events(api, t, "audit.download_aborted")
    reads = await _events(api, t, *READS)
    return Cut(got["status"], got["headers"], bytes(got["body"]), error, cut_at - ended,
               aborted[-1] if aborted else None, await _anchored(api, t),
               reads[-1] if reads else None)  # fmt: skip


READS = (
    "audit.evidence_content_read", "audit.render_file_read", "audit.render_native_read",
    "audit.render_package_read", "audit.job_package_read", "audit.report_read",
    "audit.report_package_read", "audit.preview_read",
)  # fmt: skip


def check_cut(cut: Cut, route: str, *, reason: str = "session_revoked", zip_file: bool = False,
              piece: int = CUT["api_download_chunk_bytes"]) -> None:  # fmt: skip
    """What every cut must show (plan §1.5): an error, not a completed response; fewer bytes than
    the declared Content-Length; the audit with the exact bytes sent, committed and anchored."""
    assert cut.status == 200, cut.status
    assert isinstance(cut.error, DownloadCut) or any(
        isinstance(e, DownloadCut) for e in getattr(cut.error, "exceptions", ())
    ), repr(cut.error)
    assert cut.declared > 2 * piece, f"the object is too small to cut: {cut.declared}"
    assert 0 < len(cut.received) < cut.declared  # the client can tell: short of Content-Length
    assert cut.after_revocation < CUT["api_stream_recheck_seconds"] + 2
    assert cut.audit is not None, "no download_aborted"
    payload = cut.audit.payload
    assert (payload["route"], payload["reason"]) == (route, reason), payload
    assert (payload["bytes_sent"], payload["size"]) == (len(cut.received), cut.declared), payload
    assert cut.read is not None and payload["read_seq"] == cut.read.seq
    assert cut.anchored >= cut.audit.seq  # committed AND anchored
    if zip_file:
        assert not cut.is_zip()  # a package cut before its central directory is not a zip


async def full_length(api: Api, t: TenantCtx, path: str, query: str = "") -> bytes:
    """An uncut download: exactly its Content-Length."""
    async with api.client(t.subdomain, t.token(api.settings)) as c:
        r = await c.get(path + (f"?{query}" if query else ""))
    assert r.status_code == 200, r.text
    assert len(r.content) == int(r.headers["content-length"]), (len(r.content), r.headers)
    return r.content


@dataclass
class Steady:
    declared: int
    received: int
    error: BaseException | None
    last_piece_after_end: float  # seconds from the session's end to the last piece received
    audit: Any


async def steady_cut(api: Api, t: TenantCtx, path: str, *, every: float) -> Steady:
    """A client reading one piece every ``every`` seconds, with the PRODUCTION re-check bound
    (``api_stream_recheck_seconds`` 15): its session is revoked after the first piece."""
    api = tuned(api, api_stream_recheck_seconds=15.0, api_download_chunk_bytes=1024)
    holder = await _holder(api, t, "revoke")
    app = create_app(api.settings, api.resources)
    state: dict[str, Any] = {"bytes": 0, "ended": None, "last": None}
    first = asyncio.Event()
    requested = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if requested.is_set():
            await asyncio.Event().wait()
        requested.set()
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            state["declared"] = int(dict(message["headers"])[b"content-length"])
        elif message.get("body"):
            await asyncio.sleep(every)  # a slow client: the server's write completes later
            state["bytes"] += len(message["body"])
            state["last"] = time.monotonic()
            first.set()

    task = asyncio.create_task(app(_scope(host_of(api, t), path, "", holder.headers), receive,
                                   send))  # fmt: skip
    await asyncio.wait_for(first.wait(), 60)
    await holder.end()
    ended = time.monotonic()
    error: BaseException | None = None
    try:
        await asyncio.wait_for(task, 60)
    except Exception as exc:
        error = exc
    aborted = await _events(api, t, "audit.download_aborted")
    return Steady(state["declared"], state["bytes"], error, state["last"] - ended,
                  aborted[-1] if aborted else None)  # fmt: skip


# ------------------------------------------------------------------ through a real server
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def served(app: Any) -> AsyncIterator[int]:
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


@dataclass
class ClientSaw:
    declared: int
    received: bytes
    error: BaseException | None


async def cut_through_uvicorn(api: Api, t: TenantCtx, path: str, query: str = "") -> ClientSaw:
    api = tuned(api, **CUT)
    holder = await _holder(api, t, "revoke")
    pause = _Pause()
    app = pause.wrap(create_app(api.settings, api.resources))
    received = bytearray()
    error: BaseException | None = None
    async with served(app) as port, httpx.AsyncClient(timeout=30) as client:
        url = f"http://127.0.0.1:{port}{path}" + (f"?{query}" if query else "")
        async with client.stream("GET", url, headers={"host": host_of(api, t),
                                                      **holder.headers}) as r:  # fmt: skip
            assert r.status_code == 200
            declared = int(r.headers["content-length"])
            stream = r.aiter_raw()
            try:
                received += await anext(stream)  # the first piece (held behind it: the rest)
                await holder.end()
                await asyncio.sleep(api.resources.watch.interval)
                pause.release.set()
                async for chunk in stream:
                    received += chunk
            except httpx.HTTPError as exc:
                error = exc
    return ClientSaw(declared, bytes(received), error)


def unique() -> str:
    return uuid.uuid4().hex
