"""In-flight downloads end when the session does (M17 step 5b, plan §1.5, ADR 0016 A5, decision D3).

Every content route (evidence content, render file, native, render package, job package, report
file, report package, preview) wraps its body in :func:`guarded`:

- **Re-checked while it streams.** At piece boundaries (never inside a write), at most
  ``SessionWatch.interval`` apart, the session row (cookie callers: revoked, idle or absolute
  expiry, by the database clock) or the principal's active flag (bearer callers: an IdP's token
  cannot be revoked by us) is asked through the shared per-process :class:`~edisc_api.stream.SessionWatch`,
  the same one the job streams use: revocation, expiry or deactivation cuts the transfer within
  ``api_stream_recheck_seconds`` (<= 15 s) of streaming.
- **A cut is never a complete-looking file.** The response declares its exact ``Content-Length``;
  the cut raises :class:`DownloadCut` after the response started, so the server closes the
  connection without the rest (HTTP/1.1: fewer bytes than declared, no terminating chunk; HTTP/2:
  ``RST_STREAM``) and the client sees an error. A package is cut before its central directory,
  so the bytes received are not a valid zip.
- **Audited.** ``download_aborted`` (the route's operationId, what was served, ``bytes_sent``,
  ``size``, the reason, the read's audit sequence number) is committed and anchored BEFORE the
  connection is cut. Bytes already sent cannot be recalled: the audit says how many.
- Bodies are re-cut into pieces of at most ``api_download_chunk_bytes`` so a large chunk (a whole
  preview page, a big S3 part) still has boundaries where the check can run.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from edisc_api import audit
from edisc_api.auth import Caller
from edisc_api.safe_errors import ResponseCut
from edisc_api.stream import shielded
from edisc_api.stream_events import EndReason
from edisc_core.logs import get_logger
from edisc_db.session import tenant_tx

if TYPE_CHECKING:
    from edisc_api.app import Resources

log = get_logger(__name__)


class DownloadCut(ResponseCut):
    """Raised after the response started: the server resets the connection (never completes it)."""

    def __init__(self, reason: EndReason) -> None:
        super().__init__(f"download cut: {reason.value}")
        self.reason = reason


@dataclass(frozen=True)
class Download:
    route: str  # the operationId
    served: Mapping[str, Any]  # ids of what is served (as the read's audit records them)
    size: int  # the Content-Length declared
    read_seq: int  # the audit event of the read


async def guarded(
    chunks: AsyncIterator[bytes], *, res: Resources, caller: Caller, request_id: str,
    download: Download,
) -> AsyncIterator[bytes]:  # fmt: skip
    """``chunks`` re-cut into pieces, the session re-checked between them (module docstring)."""
    watch, piece = res.watch, res.settings.api_download_chunk_bytes
    sent, checked = 0, time.monotonic()
    async for chunk in chunks:
        for start in range(0, len(chunk), piece):
            if time.monotonic() - checked >= watch.interval:
                reason = await shielded(watch.check(caller))
                checked = time.monotonic()
                if reason is not None:
                    await shielded(_record(res, caller, request_id, download, sent, reason))
                    log.warning("download cut", route=download.route, reason=reason.value,
                                bytes_sent=sent, size=download.size)  # fmt: skip
                    raise DownloadCut(reason)
            part = chunk if len(chunk) <= piece else chunk[start : start + piece]
            yield part
            sent += len(part)


async def one_chunk(body: bytes) -> AsyncIterator[bytes]:
    """A body already in memory (the preview page), streamed through :func:`guarded`."""
    yield body


async def _record(
    res: Resources, caller: Caller, request_id: str, download: Download, sent: int,
    reason: EndReason,
) -> None:  # fmt: skip
    async with tenant_tx(res.sessions, caller.tenant_id) as s:
        event = await audit.record(
            s, tenant_id=caller.tenant_id, actor=caller.actor, event_type="download_aborted",
            payload={"route": download.route, **download.served, "bytes_sent": sent,
                     "size": download.size, "reason": reason.value,
                     "read_seq": download.read_seq},
            request_id=request_id,
        )  # fmt: skip
    # committed AND anchored before the connection is cut, like the read it ends
    await audit.anchor_now(res.sessions, res.s3, res.settings, caller.tenant_id, event)
