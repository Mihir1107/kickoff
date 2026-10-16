"""A Server-Sent Events client for the API's ASGI app, with control over pacing (M17 step 5).

httpx's ASGI transport collects a whole body before returning it, so a stream that never ends could
not be read with it. :class:`SseStream` calls the app itself: every body chunk the app sends is
parsed into events as it arrives, ``pause()`` makes the app's ``send`` block (a client that stopped
reading: the server's write never completes), and closing sends ``http.disconnect``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI


@dataclass(frozen=True)
class Event:
    id: str | None
    name: str
    data: dict[str, Any]


@dataclass
class SseStream:
    app: FastAPI
    host: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    raw: bytearray = field(default_factory=bytearray)
    events: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)
    all_events: list[Event] = field(default_factory=list)
    chunks: int = 0
    keepalives: list[float] = field(default_factory=list)  # monotonic time of each keepalive
    _buffer: str = ""
    _started: asyncio.Event = field(default_factory=asyncio.Event)
    _disconnect: asyncio.Event = field(default_factory=asyncio.Event)
    _flowing: asyncio.Event = field(default_factory=asyncio.Event)
    _done: asyncio.Event = field(default_factory=asyncio.Event)
    _keepalive: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None
    _sent_request: bool = False

    async def __aenter__(self) -> SseStream:
        self._flowing.set()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": self.path, "raw_path": self.path.encode(), "query_string": b"",
            "root_path": "", "client": ("127.0.0.1", 50000), "server": (self.host, 80),
            "headers": [(b"host", self.host.encode()),
                        *((k.lower().encode(), v.encode()) for k, v in self.headers.items())],
        }  # fmt: skip
        self._task = asyncio.create_task(self._run(scope))
        await asyncio.wait_for(self._started.wait(), 30)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _run(self, scope: dict[str, Any]) -> None:
        try:
            await self.app(scope, self._receive, self._send)
        finally:
            self._done.set()
            self._started.set()

    async def _receive(self) -> dict[str, Any]:
        if not self._sent_request:
            self._sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.response_headers = {k.decode(): v.decode() for k, v in message["headers"]}
            self._started.set()
            return
        if message["type"] != "http.response.body":
            return
        await self._flowing.wait()  # a paused client: the server's write does not complete
        body: bytes = message.get("body", b"")
        self.raw += body
        self.chunks += 1
        self._buffer += body.decode()
        while "\n\n" in self._buffer:
            block, self._buffer = self._buffer.split("\n\n", 1)
            if block == ": keepalive":
                self.keepalives.append(time.monotonic())
                self._keepalive.set()
            event = _parse(block)
            if event is not None:
                self.all_events.append(event)
                self.events.put_nowait(event)
        if not message.get("more_body", False):
            self._done.set()

    def pause(self) -> None:
        self._flowing.clear()

    def resume(self) -> None:
        self._flowing.set()

    async def next(self, within: float = 10) -> Event:
        return await asyncio.wait_for(self.events.get(), within)

    async def until(self, name: str, within: float = 30) -> list[Event]:
        """Events up to and including the first one called ``name``."""
        got: list[Event] = []
        while True:
            event = await self.next(within)
            got.append(event)
            if event.name == name:
                return got

    async def keepalive(self, within: float = 10) -> float:
        """When the first keepalive (written after each session re-check) arrived."""
        await asyncio.wait_for(self._keepalive.wait(), within)
        return self.keepalives[0]

    async def ended(self, within: float = 10) -> bool:
        try:
            await asyncio.wait_for(self._done.wait(), within)
        except TimeoutError:
            return False
        return True

    async def close(self) -> None:
        self._disconnect.set()
        self._flowing.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 10)
            except TimeoutError:
                self._task.cancel()


def _parse(block: str) -> Event | None:
    fields: dict[str, str] = {}
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        fields[name] = value[1:] if value.startswith(" ") else value
    if "event" not in fields:
        return None  # retry: or a comment
    return Event(fields.get("id"), fields["event"], json.loads(fields.get("data") or "{}"))
