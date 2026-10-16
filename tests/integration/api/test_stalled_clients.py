"""Stalled clients hold nothing pooled and starve nobody (the fixes before M17 step 6).

50 native downloads (1 MiB each) and 10 job streams whose clients stop reading: the server's
writes to them block. Meanwhile the API's database pool has no connection checked out, its Redis
pool none in use, and another client downloads the same native whole and reads its permissions:
no 503 for anyone, within a bound far below the stall timeout. 50 is the S3 client's pool
(``max_pool_connections``): before ``edisc_evidence.worm.read_ranges`` each stalled download kept
an S3 GetObject body, so a pooled connection, open across its writes, and the 51st reader (and the
anchor every content read writes to S3) waited for one. Then the stall bound cuts every stalled
download and stream (reason ``stalled``, audited)."""

from __future__ import annotations

import asyncio
import io
import itertools
import struct
import time
import zipfile
from typing import Any

from sqlalchemy import event

from edisc_api.app import create_app

from .conftest import Api, TenantCtx
from .cut import _events, _Pause, _scope
from .sessions_flow import host_of
from .sse import SseStream
from .test_job_stream import new_job, tuned
from .test_render_natives import _rendered
from .test_renders import render_worker

__all__ = ["render_worker"]

STALLED = 50  # 40 natives + 10 render packages (embed: object members read from S3)
PACKAGES = 10
STREAMS = 10
SERVED_WITHIN = 20.0  # seconds for the other client's whole download; the stall bound is 60


def _inside_a_large_member(package: bytes) -> int:
    """An offset just past the start of the data of the first member over 1 MiB."""
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        member = next(i for i in zf.infolist() if i.file_size > (1 << 20))
        header = package[member.header_offset : member.header_offset + 30]
        name_len, extra_len = struct.unpack("<HH", header[26:30])
    return member.header_offset + 30 + name_len + extra_len + 1


def _s3_in_use(s3: Any) -> int:
    """Connections the S3 client's pool has lent out (aiobotocore over aiohttp)."""
    sessions = s3._endpoint.http_session._sessions.values()
    return sum(len(session.connector._acquired) for session in sessions)


async def _stalled_download(
    app: Any, host: str, path: str, token: str, after: int = 0
) -> tuple[_Pause, Any]:
    pause = _Pause(after=after)
    requested = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if requested.is_set():
            await asyncio.Event().wait()
        requested.set()
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        return None

    route, _, query = path.partition("?")
    scope = _scope(host, route, query, {"authorization": f"Bearer {token}"})
    task = asyncio.create_task(pause.wrap(app)(scope, receive, send))
    return pause, task


async def test_stalled_clients_hold_nothing_pooled_and_others_are_served(
    api: Api, tenant: TenantCtx, render_worker: None
) -> None:
    _, render, natives = await _rendered(api, tenant)
    api = tuned(api, download_stall_seconds=60.0)
    app = create_app(api.settings, api.resources)
    host, token = host_of(api, tenant), tenant.token(api.settings)
    path = f"/v1/renders/{render['id']}/natives/{natives[0]['sha256']}/content"
    package = f"/v1/renders/{render['id']}/package?outputs=embed"
    # every checkout of the API's database pool: (connection record) -> which checkout it is
    pool = api.sessions.kw["bind"].sync_engine
    out: dict[int, int] = {}
    count = itertools.count()

    def checkout(_dbapi: Any, record: Any, _proxy: Any) -> None:
        out[id(record)] = next(count)

    def checkin(_dbapi: Any, record: Any) -> None:
        out.pop(id(record), None)

    event.listen(pool, "checkout", checkout)
    event.listen(pool, "checkin", checkin)
    async with api.client(tenant.subdomain, token) as c:
        whole_package = (await c.get(package)).content
    # a package stalls INSIDE an object member larger than one S3 read (1 MiB): a member read in
    # one piece is fully read (its connection back in the pool) before it is written, so stalling
    # anywhere else would prove nothing about the object members
    inside = _inside_a_large_member(whole_package)
    stalled = [
        await _stalled_download(app, host, package, token, after=inside) for _ in range(PACKAGES)
    ] + [await _stalled_download(app, host, path, token) for _ in range(STALLED - PACKAGES)]
    await asyncio.wait_for(asyncio.gather(*(p.first.wait() for p, _ in stalled)), 120)
    job = await new_job(api, tenant)
    streams = [SseStream(app, host, f"/v1/jobs/{job.id}/stream",
                         {"authorization": f"Bearer {token}"}) for _ in range(STREAMS)]  # fmt: skip
    for sse in streams:
        await sse.__aenter__()
        await sse.next()
        sse.pause()  # the next write (the keepalive) blocks
    try:
        # the streams' feed polls and session checks use the pool in short transactions while
        # everything is stalled; nothing may STAY checked out: whatever is out now must be back
        # after several poll cycles (an instant count would also see a poll in flight)
        before = dict(out)
        await asyncio.sleep(3 * api.settings.api_stream_poll_seconds + 1)
        held = {k for k, n in before.items() if out.get(k) == n}
        assert not held, "a stalled response holds a database connection"
        in_use = list(api.resources.redis.connection_pool._get_in_use_connections())
        assert in_use == [], "a stalled response holds a Redis connection"
        assert _s3_in_use(api.s3) == 0, "a stalled response holds a pooled S3 connection"
        started = time.monotonic()
        async with api.client(tenant.subdomain, token) as c:
            whole = await c.get(path)
            me = await c.get("/v1/me/permissions")
        served = time.monotonic() - started
        assert (whole.status_code, me.status_code) == (200, 200), (whole.status_code, me.text)
        assert len(whole.content) == natives[0]["size"]
        assert served < SERVED_WITHIN, served
        assert all(not t.done() for _, t in stalled)  # still stalled, not ended by the reads
    finally:
        for _, task in stalled:
            task.cancel()
        await asyncio.gather(*(t for _, t in stalled), return_exceptions=True)
        for sse in streams:
            sse.resume()
            await sse.close()
        event.remove(pool, "checkout", checkout)
        event.remove(pool, "checkin", checkin)


async def test_the_stall_bound_cuts_a_stalled_stream(api: Api, tenant: TenantCtx) -> None:
    """A stream whose client reads nothing for download_stall_seconds is cut and audited (the
    content routes' stall cuts are in their own files, through cut.cut_every_way)."""
    api = tuned(api, download_stall_seconds=1.0, api_stream_recheck_seconds=0.4)
    job = await new_job(api, tenant)
    app = create_app(api.settings, api.resources)
    sse = SseStream(app, host_of(api, tenant), f"/v1/jobs/{job.id}/stream",
                    {"authorization": f"Bearer {tenant.token(api.settings)}"})  # fmt: skip
    await sse.__aenter__()
    await sse.next()
    sse.pause()  # the keepalive after the next re-check (0.2 s) is never read
    try:
        assert sse._task is not None
        done = await asyncio.wait_for(asyncio.gather(sse._task, return_exceptions=True), 10)
        assert type(done[0]).__name__ == "DownloadCut", repr(done[0])
    finally:
        sse.resume()
        if not sse._task.done():  # (the cut task's exception was read above)
            await sse.close()
    aborted = await _events(api, tenant, "audit.download_aborted")
    assert aborted and aborted[-1].payload["route"] == "stream_job"
    assert aborted[-1].payload["reason"] == "stalled"
