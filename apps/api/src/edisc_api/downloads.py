"""In-flight downloads end with the session, the permission or the client (M17 step 5b, plan §1.5,
ADR 0016 A5, decision D3; the fixes before step 6).

Every content route (evidence content, render file, native, render package, job package, report
file, report package, preview) answers a :class:`GuardedResponse` over :func:`guarded`; the job
stream answers one too (stall timeout only: it re-checks by itself):

- **Re-checked while it streams.** At piece boundaries (never inside a write), at most
  ``SessionWatch.interval`` apart: the session row (cookie callers: revoked, idle or absolute
  expiry, by the database clock) or the principal's active flag (bearer callers: an IdP's token
  cannot be revoked by us), through the shared per-process :class:`~edisc_api.stream.SessionWatch`,
  THEN the read permission on the object (``permitted``, never cached). Revocation, expiry,
  deactivation or a lost permission cuts the transfer within ``api_stream_recheck_seconds``
  (<= 15 s) of streaming.
- **A stalled client is cut.** Every write to the server is timed: one that has not completed
  after ``download_stall_seconds`` (the client read nothing for that long) cuts with reason
  ``stalled``. Nothing pooled is held meanwhile: S3 bytes come range by range
  (``edisc_evidence.worm.read_ranges``), database work is one short transaction per page, never
  across a write (``tests/integration/api/test_stalled_clients.py`` measures it).
- **A cut is never a complete-looking file.** The response declares its exact ``Content-Length``;
  the cut raises :class:`DownloadCut` after the response started, so the server closes the
  connection without the rest (HTTP/1.1: fewer bytes than declared, no terminating chunk; HTTP/2:
  ``RST_STREAM``) and the client sees an error. A package is cut before its central directory, so
  the bytes received are not a valid zip.
- **Audited.** ``download_aborted`` (the route's operationId, what was served, ``bytes_sent``: the
  bytes whose write completed, ``size``, the reason, the read's audit sequence number) is committed
  and anchored BEFORE the connection is cut. Bytes already sent cannot be recalled: the audit says
  how many.
- Bodies are re-cut into pieces of at most ``api_download_chunk_bytes`` so a large chunk (a whole
  preview page, a big S3 range) still has boundaries where the checks can run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from fastapi.responses import StreamingResponse
from starlette.types import Message, Send

from edisc_api import audit
from edisc_api.auth import Caller
from edisc_api.authz import Permission, Scope, authorize
from edisc_api.errors import ApiError
from edisc_api.safe_errors import ResponseCut
from edisc_api.stream import shielded
from edisc_core.logs import get_logger
from edisc_db.session import tenant_tx

if TYPE_CHECKING:
    from edisc_api.app import Resources

log = get_logger(__name__)

Permitted = Callable[[], Awaitable[bool]]


class CutReason(StrEnum):
    SESSION_REVOKED = "session_revoked"
    SESSION_EXPIRED = "session_expired"
    PRINCIPAL_DEACTIVATED = "principal_deactivated"
    PERMISSION_LOST = "permission_lost"  # the read permission on the object is gone
    STALLED = "stalled"  # no write progress for download_stall_seconds


class DownloadCut(ResponseCut):
    """Raised after the response started: the server resets the connection (never completes it)."""

    def __init__(self, reason: CutReason) -> None:
        super().__init__(f"download cut: {reason.value}")
        self.reason = reason


@dataclass(frozen=True)
class Download:
    route: str  # the operationId
    served: Mapping[str, Any]  # ids of what is served (as the read's audit records them)
    size: int | None  # the Content-Length declared (None: a stream)
    read_seq: int | None  # the audit event of the read (None: a stream, not a content read)


def still_permitted(
    res: Resources, caller: Caller, permission: Permission, scope: Scope
) -> Permitted:
    """The route's own read authorization, asked again (one short transaction)."""

    async def check() -> bool:
        async with tenant_tx(res.sessions, caller.tenant_id) as s:
            try:
                await authorize(s, caller, permission, scope)
            except ApiError:
                return False
        return True

    return check


class Guard:
    """One response's body: pieces, re-checks, the bytes sent, the cut (module docstring)."""

    def __init__(
        self, chunks: AsyncIterator[bytes], *, res: Resources, caller: Caller, request_id: str,
        download: Download, permitted: Permitted | None,
    ) -> None:  # fmt: skip
        self.chunks, self.res, self.caller = chunks, res, caller
        self.request_id, self.download, self.permitted = request_id, download, permitted
        self.sent = 0

    async def body(self) -> AsyncIterator[bytes]:
        watch, piece = self.res.watch, self.res.settings.api_download_chunk_bytes
        checked = time.monotonic()
        async for chunk in self.chunks:
            for start in range(0, len(chunk), piece):
                if self.permitted is not None and time.monotonic() - checked >= watch.interval:
                    ended = await shielded(watch.check(self.caller))
                    reason = CutReason(ended.value) if ended is not None else None
                    if reason is None and not await shielded(self.permitted()):
                        reason = CutReason.PERMISSION_LOST
                    checked = time.monotonic()
                    if reason is not None:
                        await self.cut(reason)
                part = chunk if len(chunk) <= piece else chunk[start : start + piece]
                yield part
                self.sent += len(part)  # its write completed (the server awaited it)

    async def cut(self, reason: CutReason) -> None:
        await shielded(self._record(reason))
        log.warning("download cut", route=self.download.route, reason=reason.value,
                    bytes_sent=self.sent, size=self.download.size)  # fmt: skip
        raise DownloadCut(reason)

    async def _record(self, reason: CutReason) -> None:
        res, caller = self.res, self.caller
        async with tenant_tx(res.sessions, caller.tenant_id) as s:
            event = await audit.record(
                s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="download_aborted",
                payload={"route": self.download.route, **self.download.served,
                         "bytes_sent": self.sent, "size": self.download.size,
                         "reason": reason.value, "read_seq": self.download.read_seq},
                request_id=self.request_id,
            )  # fmt: skip
        # committed AND anchored before the connection is cut, like the read it ends
        await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, event)


def guarded(
    chunks: AsyncIterator[bytes], *, res: Resources, caller: Caller, request_id: str,
    download: Download, permitted: Permitted | None,
) -> Guard:  # fmt: skip
    """``permitted``: the read permission re-check of a content route; None for the job stream
    (it re-checks session and permission itself and is only timed here)."""
    return Guard(chunks, res=res, caller=caller, request_id=request_id, download=download,
                 permitted=permitted)  # fmt: skip


class GuardedResponse(StreamingResponse):
    """A :class:`Guard`'s body, every write to the server timed (``download_stall_seconds``)."""

    def __init__(self, guard: Guard, **kwargs: Any) -> None:
        super().__init__(guard.body(), **kwargs)
        self.guard = guard

    async def stream_response(self, send: Send) -> None:
        stall = self.guard.res.settings.download_stall_seconds

        async def timed(message: Message) -> None:
            try:
                await asyncio.wait_for(send(message), stall)
            except TimeoutError:
                await self.guard.cut(CutReason.STALLED)

        try:
            await super().stream_response(timed)
        finally:
            body = self.body_iterator
            if isinstance(body, AsyncIterator) and hasattr(body, "aclose"):
                await body.aclose()  # the route's generator ends now, not at garbage collection


async def one_chunk(body: bytes) -> AsyncIterator[bytes]:
    """A body already in memory (the preview page), streamed through :func:`guarded`."""
    yield body
