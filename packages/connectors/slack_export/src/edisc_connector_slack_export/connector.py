"""Collecting from an uploaded, validated Slack export (ADR 0014 sections 4, 6 and 7).

Thin, like every connector: it enumerates, reads raw bytes and downloads files; the normalizer
interprets them (``dialect = "export"``).

- **Units** are day files: ``(conversation, file date)``, keyed like every other conversation-day unit.
  The file date is only a HINT (R4): a file is enumerated when its date +/- 1 day overlaps the scope, and
  every message is placed and scoped by its own ``ts`` downstream.
- **Batches** are entries of the LOCKED zip: the bytes are read from the pinned version (CRC, size and
  SHA-256 checked) and handed over with an ``EntryRef``, so the pipeline references the entry instead of
  writing a copy. The cursor is a position in a deterministic plan (the unit's day file, then thread
  context), so resume is exact.
- **Thread context across day files**: replies sit in the file of their own day. The thread index
  built at validation (``export_threads``) says which entries hold a thread's parent and replies, so the
  thread-parent policy is applied without rereading the archive; a context batch carries the entry and
  the ``ts`` of the thread's messages in it (``select``).
- **Files**: the export only links them. Links carry access tokens: they are registered as secrets,
  never logged, never stored outside the evidence, never passed to Temporal (only file ids are). Every
  download takes a ``slack_export.file`` rate-limit token. An expired, revoked or unreachable link is a
  ``FileUnavailableError`` with its reason: a recorded file gap, never a stall.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_slack_export.archive_access import (
    LockedExport,
    archive_source,
    entry_from_row,
    load_export,
    read_verified,
)
from edisc_connector_slack_export.layout import BLIND_SPOTS_ALL, BLIND_SPOTS_PUBLIC_ONLY
from edisc_connectors_base.protocol import Limiter
from edisc_connectors_base.ratelimit import BucketKey
from edisc_connectors_base.types import (
    BatchKind,
    CollectionScope,
    Connection,
    ConnectionInfo,
    Cursor,
    EntryRef,
    FileUnavailableError,
    FileUnavailableReason,
    InvalidCursorError,
    RawBatch,
    ThreadParentPolicy,
    WorkUnit,
)
from edisc_core.redaction import register_secret
from edisc_core.schemas import ScopeType
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc
from edisc_db.session import tenant_tx

FILE_REFUSALS = {
    401: FileUnavailableReason.PERMISSION,  # revoked
    403: FileUnavailableReason.PERMISSION,
    404: FileUnavailableReason.DELETED,
    410: FileUnavailableReason.EXPIRED_URL,
}


MIN_SECRET_LEN = (
    12  # shorter query values are not tokens; registering them would scrub ordinary text
)
MAX_LINKS = 100_000


DAY_ENTRY = (
    "SELECT f.idx, f.name, f.kind, f.method, f.flags, f.crc32, f.compressed_size, f.uncompressed_size, f.local_header_offset, f.raw_name, f.name_encoding,"
    " d.elements, d.parse_error FROM export_entries f"
    " JOIN export_conversations c ON c.export_id = f.export_id AND c.folder = f.folder"
    " LEFT JOIN export_day_files d ON d.export_id = f.export_id AND d.entry_idx = f.idx"
    " WHERE f.export_id = :e AND f.kind = 'day' AND c.conversation_id = :c AND f.hint_day = :d"
)
ENTRY_BY_IDX = (
    "SELECT f.idx, f.name, f.kind, f.method, f.flags, f.crc32, f.compressed_size, f.uncompressed_size, f.local_header_offset, f.raw_name, f.name_encoding"
    " FROM export_entries f WHERE f.export_id = :e AND f.idx = :i"
)
USERS_ENTRY = (
    "SELECT f.idx, f.name, f.kind, f.method, f.flags, f.crc32, f.compressed_size, f.uncompressed_size, f.local_header_offset, f.raw_name, f.name_encoding"
    " FROM export_entries f WHERE f.export_id = :e AND f.kind = 'metadata'"
    " AND (f.name = 'users.json' OR f.name = :wrapped) ORDER BY f.idx LIMIT 1"
)


class UnsupportedScopeError(ValueError):
    """Exports carry no membership history: only channel scopes (one conversation or all)."""


@dataclass(frozen=True)
class _Step:
    kind: BatchKind
    entry_idx: int
    select: frozenset[str] | None
    thread_ts: str | None


def _encode(index: int) -> Cursor:
    return base64.urlsafe_b64encode(json.dumps({"v": 1, "i": index}).encode()).decode()


def _decode(cursor: Cursor | None) -> int:
    if cursor is None:
        return 0
    try:
        doc = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        index = doc["i"]
        if doc.get("v") != 1 or not isinstance(index, int) or index < 0:
            raise InvalidCursorError(f"unsupported cursor {cursor!r}")
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidCursorError(f"malformed cursor {cursor!r}") from exc
    return index


def _ts_seconds(ts: str) -> float:
    return float(ts)


class SlackExportConnector:
    source = "slack_export"
    version = "0.1.0"
    item_source = "slack"  # an exported message IS the Slack message: same identity as via the API
    dialect = "export"
    archive_backed = True

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        s3: S3Client,
        settings: Settings,
        limiter: Limiter,
        http: httpx.AsyncClient,
    ) -> None:
        self._sessions, self._s3, self._settings = sessions, s3, settings
        self._limiter, self._http = limiter, http
        # file id -> export link, filled while a batch is read and used right after by the same
        # activity (the pipeline downloads a batch's files before normalizing it). Never persisted;
        # bounded (oldest dropped first).
        self._links: OrderedDict[tuple[uuid.UUID, str], str] = OrderedDict()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _export_id(conn: Connection) -> uuid.UUID:
        return uuid.UUID(str(conn.config["export_id"]))

    async def _export(self, conn: Connection) -> LockedExport:
        async with tenant_tx(self._sessions, conn.tenant_id) as s:
            export = await load_export(s, self._export_id(conn))
        if export.status != "ready":
            raise ValueError(f"export {export.export_id} is {export.status}, not ready")
        return export

    async def _entry_rows(self, conn: Connection, sql: str, params: dict[str, Any]) -> list[Any]:
        async with tenant_tx(self._sessions, conn.tenant_id) as s:
            return list((await s.execute(text(sql), params)).all())

    # ------------------------------------------------------------------ protocol
    async def validate_connection(self, conn: Connection) -> ConnectionInfo:
        export = await self._export(conn)
        spots = tuple(export.findings.get("blind_spots") or BLIND_SPOTS_ALL)
        if export.tier == "public_only" and not set(BLIND_SPOTS_PUBLIC_ONLY) <= set(spots):
            spots = spots + BLIND_SPOTS_PUBLIC_ONLY
        return ConnectionInfo(export.tier or "unknown", ("export",), spots, True)

    async def enumerate(self, conn: Connection, scope: CollectionScope) -> AsyncIterator[WorkUnit]:
        if scope.scope_type is not ScopeType.CHANNEL:
            raise UnsupportedScopeError(
                f"{scope.scope_type.value} scopes need membership history, which exports lack"
            )
        first = ensure_utc(scope.date_from).date() - timedelta(days=1)
        last = (ensure_utc(scope.date_to) - timedelta(microseconds=1)).date() + timedelta(days=1)
        rows = await self._entry_rows(
            conn,
            "SELECT c.conversation_id, f.hint_day FROM export_entries f"
            " JOIN export_conversations c ON c.export_id = f.export_id AND c.folder = f.folder"
            " WHERE f.export_id = :e AND f.kind = 'day' AND f.hint_day BETWEEN :a AND :b"
            " AND (CAST(:c AS text) = '*' OR c.conversation_id = :c)"
            " ORDER BY c.conversation_id, f.hint_day",
            {"e": self._export_id(conn), "a": first, "b": last, "c": scope.external_id},
        )
        for row in rows:
            yield WorkUnit(row.conversation_id, row.hint_day)

    async def _day_entry(self, conn: Connection, unit: WorkUnit) -> Any:
        rows = await self._entry_rows(
            conn,
            DAY_ENTRY,
            {"e": self._export_id(conn), "c": unit.conversation_id, "d": unit.day},
        )
        if len(rows) != 1:
            raise InvalidCursorError(f"{unit.unit_key}: {len(rows)} day files (expected one)")
        return rows[0]

    async def expected_count(self, conn: Connection, unit: WorkUnit) -> int | None:
        """The number of elements in the day file (counted at validation); None if it did not parse."""
        row = await self._day_entry(conn, unit)
        return None if row.elements is None else int(row.elements)

    async def _plan(
        self, conn: Connection, unit: WorkUnit, body: bytes, entry_idx: int, scope: CollectionScope
    ) -> list[_Step]:
        """History first, then the thread context the policy asks for (same rules as the Web API
        connector): replies here whose parent is before the range -> the parent (parent_only) or the
        whole thread; parents here with replies at/after the end of the range -> the whole thread."""
        steps = [_Step(BatchKind.HISTORY, entry_idx, None, None)]
        policy = scope.thread_parent_policy
        if policy is ThreadParentPolicy.REPLIES_ONLY:
            return steps
        try:
            messages = [m for m in json.loads(body) if isinstance(m, dict)]
        except ValueError:
            return steps  # the history batch fails loudly in the normalizer; no context to plan
        start = ensure_utc(scope.date_from).timestamp()
        end = ensure_utc(scope.date_to).timestamp()
        before: list[str] = []
        after: list[str] = []
        for m in messages:
            ts, thread_ts = m.get("ts"), m.get("thread_ts")
            if not isinstance(ts, str) or not isinstance(thread_ts, str):
                continue
            if thread_ts != ts and _ts_seconds(thread_ts) < start and thread_ts not in before:
                before.append(thread_ts)
            elif thread_ts == ts and policy is ThreadParentPolicy.INCLUDE_PARENT_AND_THREAD:
                after.append(ts)
        threads: list[tuple[str, bool]] = [
            (t, policy is ThreadParentPolicy.INCLUDE_PARENT_ONLY) for t in before
        ]
        if after:
            late = await self._entry_rows(
                conn,
                "SELECT DISTINCT thread_ts FROM export_threads WHERE export_id = :e"
                " AND conversation_id = :c AND thread_ts = ANY(:t) AND ts <> thread_ts"
                " AND CAST(ts AS numeric) >= :end",
                {"e": self._export_id(conn), "c": unit.conversation_id, "t": after, "end": end},
            )
            threads += [(r.thread_ts, False) for r in sorted(late, key=lambda r: r.thread_ts)]
        for thread_ts, parent_only in threads:
            members = await self._entry_rows(
                conn,
                "SELECT entry_idx, ts FROM export_threads WHERE export_id = :e"
                " AND conversation_id = :c AND thread_ts = :t ORDER BY entry_idx, element_idx",
                {"e": self._export_id(conn), "c": unit.conversation_id, "t": thread_ts},
            )
            by_entry: dict[int, set[str]] = {}
            for r in members:
                if parent_only and r.ts != thread_ts:
                    continue
                if r.entry_idx != entry_idx:  # this file's own messages are in the history batch
                    by_entry.setdefault(int(r.entry_idx), set()).add(r.ts)
            steps += [
                _Step(BatchKind.THREAD_CONTEXT, idx, frozenset(ts_set), thread_ts)
                for idx, ts_set in sorted(by_entry.items())
            ]
        return steps

    async def fetch(
        self, conn: Connection, unit: WorkUnit, cursor: Cursor | None, *, scope: CollectionScope
    ) -> AsyncIterator[RawBatch]:
        export = await self._export(conn)
        src = archive_source(self._s3, self._settings, export)
        day = await self._day_entry(conn, unit)
        day_entry = entry_from_row(day)
        body, digest = await read_verified(src, day_entry, export)
        steps = await self._plan(conn, unit, body, day_entry.index, scope)
        index = _decode(cursor)
        if index > len(steps):
            raise InvalidCursorError(f"cursor beyond the end of {unit.unit_key}")
        while index < len(steps):
            step = steps[index]
            if step.entry_idx == day_entry.index:
                entry, data, dg = day_entry, body, digest
            else:
                row = (
                    await self._entry_rows(
                        conn,
                        ENTRY_BY_IDX,
                        {"e": self._export_id(conn), "i": step.entry_idx},
                    )
                )[0]
                entry = entry_from_row(row)
                data, dg = await read_verified(src, entry, export)
            self._remember_links(conn, data, step.select)
            nxt = _encode(index + 1) if index + 1 < len(steps) else None
            request = {
                "method": "export.day_file" if step.kind is BatchKind.HISTORY else "export.thread",
                "entry": entry.name,
                "cursor": _encode(index) if index else "",
                **({"thread_ts": step.thread_ts} if step.thread_ts else {}),
                **({"policy": scope.thread_parent_policy.value} if step.thread_ts else {}),
            }
            yield RawBatch(
                data,
                nxt,
                step.kind,
                request,
                entry=EntryRef(
                    export.evidence_id,
                    entry.name,
                    entry.raw_name,
                    entry.crc32,
                    entry.compressed_size,
                    dg.sha256,
                    dg.size,
                ),
                select=step.select,
            )
            index += 1

    def _remember_links(self, conn: Connection, body: bytes, select: frozenset[str] | None) -> None:
        try:
            messages = json.loads(body)
        except ValueError:
            return
        for m in messages if isinstance(messages, list) else []:
            if not isinstance(m, dict) or (select is not None and m.get("ts") not in select):
                continue
            for f in m.get("files") or []:
                if not isinstance(f, dict) or not f.get("id"):
                    continue
                link = f.get("url_private_download") or f.get("url_private")
                if isinstance(link, str) and link:
                    for _, value in parse_qsl(urlsplit(link).query):
                        if len(value) >= MIN_SECRET_LEN:
                            register_secret(value)  # the export token: scrubbed from every log line
                    key = (conn.connection_id, str(f["id"]))
                    self._links[key] = link
                    self._links.move_to_end(key)
                    while len(self._links) > MAX_LINKS:
                        self._links.popitem(last=False)

    async def fetch_directory(
        self, conn: Connection, cursor: Cursor | None
    ) -> AsyncIterator[RawBatch]:
        """``users.json`` as one directory batch (an entry reference), or an empty one if absent."""
        if _decode(cursor) > 0:
            return
        export = await self._export(conn)
        rows = await self._entry_rows(
            conn,
            USERS_ENTRY,
            {
                "e": self._export_id(conn),
                "wrapped": f"{export.findings.get('root_prefix') or ''}users.json",
            },
        )
        if not rows:
            yield RawBatch(
                b"[]", None, BatchKind.DIRECTORY, {"method": "export.users", "entry": ""}
            )
            return
        entry = entry_from_row(rows[0])
        data, dg = await read_verified(
            archive_source(self._s3, self._settings, export), entry, export
        )
        yield RawBatch(
            data,
            None,
            BatchKind.DIRECTORY,
            {"method": "export.users", "entry": entry.name},
            entry=EntryRef(
                export.evidence_id,
                entry.name,
                entry.raw_name,
                entry.crc32,
                entry.compressed_size,
                dg.sha256,
                dg.size,
            ),
        )

    async def open_file(self, conn: Connection, file_ref: str) -> AsyncIterator[bytes]:
        link = self._links.get((conn.connection_id, file_ref))
        if link is None:  # hidden, external or tombstoned files have no link in the export
            body = json.dumps({"ok": False, "error": "no_link_in_export"}).encode()
            raise FileUnavailableError(file_ref, FileUnavailableReason.EXTERNAL_OR_HIDDEN, body)
        parts = urlsplit(link)
        if (
            parts.scheme != "https"
            or (parts.hostname or "") not in self._settings.export_file_hosts
        ):
            body = json.dumps({"ok": False, "error": "file_host_not_allowed"}).encode()
            raise FileUnavailableError(file_ref, FileUnavailableReason.EXTERNAL_OR_HIDDEN, body)
        key = BucketKey(conn.tenant_id, self.source, conn.workspace_id, "file")
        await self._limiter.acquire(key)
        try:
            async with self._http.stream("GET", link, follow_redirects=False) as resp:
                reason = FILE_REFUSALS.get(resp.status_code)
                if 300 <= resp.status_code < 400:  # a login redirect: the link's token expired
                    reason = FileUnavailableReason.EXPIRED_URL
                elif reason is None and resp.status_code >= 400:
                    reason = FileUnavailableReason.UNREACHABLE
                if reason is not None:
                    body = json.dumps({"ok": False, "status": resp.status_code}).encode()
                    raise FileUnavailableError(file_ref, reason, body)
                async for chunk in resp.aiter_bytes(1 << 16):
                    yield chunk
        except httpx.TransportError as exc:  # never logs the URL: only the error type
            body = json.dumps({"ok": False, "error": type(exc).__name__}).encode()
            raise FileUnavailableError(file_ref, FileUnavailableReason.UNREACHABLE, body) from None
