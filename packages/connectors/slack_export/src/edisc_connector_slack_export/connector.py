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
  request (every redirect hop) takes a ``slack_export.file`` rate-limit token. Hosts, redirects and
  resolved addresses are checked hop by hop and the connection is pinned to the checked IP
  (``file_links``). An expired, revoked or unreachable link is a ``FileUnavailableError`` with its
  reason: a recorded file gap, never a stall.
- **Lone surrogates** (ADR 0018 §28): a conversation whose id or team (or the export's workspace)
  holds one was listed under a stand-in id and refused at validation: its units raise
  ``UncollectableUnitError`` and are reported as gaps. A message whose ``ts`` holds one cannot be an
  item: the history batch selects the others (``ts_lone_surrogate``); a ``thread_ts`` holding one
  cannot be looked up for thread context (``thread_ts_lone_surrogate``). Both are recorded on the unit
  as gap reasons, never skipped silently.
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
from edisc_connector_slack_export.file_links import Resolver, download, system_resolver
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
    UncollectableUnitError,
    WorkUnit,
)
from edisc_core.lone_surrogates import has_lone
from edisc_core.redaction import register_secret
from edisc_core.schemas import ScopeType
from edisc_core.settings import Settings
from edisc_core.time import ensure_utc
from edisc_db.session import tenant_tx

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
    gaps: tuple[str, ...] = ()


TS_LONE_SURROGATE = "ts_lone_surrogate"
THREAD_TS_LONE_SURROGATE = "thread_ts_lone_surrogate"


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
        resolver: Resolver = system_resolver,
    ) -> None:
        self._sessions, self._s3, self._settings = sessions, s3, settings
        self._limiter, self._http, self._resolver = limiter, http, resolver
        # file id -> export link, filled while a batch is read and used right after by the same
        # activity (the pipeline downloads a batch's files before normalizing it). Never persisted;
        # bounded (oldest dropped first).
        self._links: OrderedDict[tuple[uuid.UUID, str], str] = OrderedDict()
        # (export, conversation) -> (team, refused): immutable once the export is validated
        self._meta: OrderedDict[tuple[uuid.UUID, str], tuple[str | None, str | None]] = (
            OrderedDict()
        )

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
    async def check_credentials(self, conn: Connection) -> None:
        """An export is uploaded, not fetched: there are no credentials to check."""
        return None

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

    async def item_workspace(self, conn: Connection, conversation_id: str) -> str:
        """The conversation's own team when its metadata record names one (Enterprise Grid: one export,
        many workspaces), else the export's workspace (users.json). Never per message: a shared
        channel's messages carry their senders' teams *(confirm on real export)*."""
        team, _ = await self._conversation(conn, conversation_id)
        return team or conn.workspace_id

    async def _conversation(
        self, conn: Connection, conversation_id: str
    ) -> tuple[str | None, str | None]:
        """(team, refused reason) of a conversation of the export."""
        key = (self._export_id(conn), conversation_id)
        if key not in self._meta:
            rows = await self._entry_rows(
                conn,
                "SELECT team_id, refused FROM export_conversations WHERE export_id = :e"
                " AND conversation_id = :c",
                {"e": key[0], "c": conversation_id},
            )
            self._meta[key] = (rows[0].team_id, rows[0].refused) if rows else (None, None)
            while len(self._meta) > MAX_LINKS:
                self._meta.popitem(last=False)
        return self._meta[key]

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
        policy = scope.thread_parent_policy
        try:
            parsed = json.loads(body)
            messages = [m for m in parsed if isinstance(m, dict)]
        except (ValueError, TypeError):
            # the history batch fails loudly in the normalizer; no context to plan
            return [_Step(BatchKind.HISTORY, entry_idx, None, None)]
        # lone surrogates (ADR 0018 §28): a ts cannot be an item identity, a thread_ts cannot be
        # looked up; both leave the unit a gap with the reason, never a failure of the whole unit
        hostile_ts = any(isinstance(m.get("ts"), str) and has_lone(m["ts"]) for m in messages)
        hostile_thread = any(
            isinstance(m.get("thread_ts"), str) and has_lone(m["thread_ts"])
            and not (isinstance(m.get("ts"), str) and has_lone(m["ts"]))
            for m in messages
        )  # fmt: skip
        select = (
            frozenset(
                m["ts"] for m in messages if isinstance(m.get("ts"), str) and not has_lone(m["ts"])
            )
            if hostile_ts
            else None
        )
        gaps = (TS_LONE_SURROGATE,) * hostile_ts + (THREAD_TS_LONE_SURROGATE,) * hostile_thread
        steps = [_Step(BatchKind.HISTORY, entry_idx, select, None, gaps)]
        if policy is ThreadParentPolicy.REPLIES_ONLY:
            return steps
        start = ensure_utc(scope.date_from).timestamp()
        end = ensure_utc(scope.date_to).timestamp()
        before: list[str] = []
        after: list[str] = []
        for m in messages:
            ts, thread_ts = m.get("ts"), m.get("thread_ts")
            if not isinstance(ts, str) or not isinstance(thread_ts, str):
                continue
            if has_lone(ts) or has_lone(thread_ts):
                continue  # recorded above as a gap reason of the unit
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
        _, refused = await self._conversation(conn, unit.conversation_id)
        if refused is not None:
            raise UncollectableUnitError(unit.unit_key, refused)
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
                gaps=step.gaps,
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
        key = BucketKey(conn.tenant_id, self.source, conn.workspace_id, "file")
        async for chunk in download(
            self._http,
            link,
            file_ref,
            hosts=self._settings.export_file_hosts,
            max_redirects=self._settings.export_file_max_redirects,
            resolver=self._resolver,
            before_request=lambda: self._limiter.acquire(key),
        ):
            yield chunk
