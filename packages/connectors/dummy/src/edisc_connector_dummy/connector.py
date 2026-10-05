"""Dummy connector: a thin connector over the deterministic dataset, with a realistically messy API.

Connection config (``Connection.config``): ``{"spec": <DatasetSpec dict>, "epoch": <int>}``.

Messiness (all deterministic from the seed; ``messy_pagination``):
- an empty page with ``has_more: true`` in the middle of a unit;
- pages that overlap (an item repeated on the next page);
- items not in timestamp order, threads split across pages;
- thread-context pages when a reply's parent lies outside the collection range (policy-driven).

Failures (``FailureSpec``, deterministic per request): exceptions, timeouts and 429s fail the first N
attempts of a request and then succeed; drops remove items from pages while ``expected_count`` still
reports the true count (or None when the source "cannot count"). Corrupt conversations fail with an
invalid cursor from their second page; unavailable conversations fail every request. Credentials are
revoked while the connection config says ``"auth_revoked": true`` (activities re-read it from the DB).

Every request takes a rate-limit token through ``call_with_limits`` first.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from edisc_connector_dummy.dataset import Dataset, Msg, h64, unit
from edisc_connector_dummy.dialects import slack
from edisc_connector_dummy.spec import CountMode, DatasetSpec
from edisc_connectors_base.protocol import Limiter
from edisc_connectors_base.ratelimit import BucketKey, SourceThrottledError, call_with_limits
from edisc_connectors_base.types import (
    AccessLossReason,
    AuthenticationError,
    BatchKind,
    CollectionScope,
    Connection,
    ConnectionInfo,
    ConversationInaccessibleError,
    Cursor,
    FileUnavailableError,
    FileUnavailableReason,
    InvalidCursorError,
    RawBatch,
    SourceUnavailableError,
    WorkUnit,
)
from edisc_core.schemas import ScopeType
from edisc_core.time import day_bounds, ensure_utc


class DummySourceError(SourceUnavailableError):
    """An injected upstream failure (HTTP 5xx-like)."""


@dataclass(frozen=True)
class _Batch:
    kind: BatchKind
    messages: tuple[Msg, ...]
    request: dict[str, str]


# the reasons a Web API file download is refused (a fixed list: the oracle must not change when a
# reason is added for other sources)
DUMMY_FILE_REASONS = (
    FileUnavailableReason.DELETED,
    FileUnavailableReason.EXTERNAL_OR_HIDDEN,
    FileUnavailableReason.EXPIRED_URL,
    FileUnavailableReason.PERMISSION,
)


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


class DummyConnector:
    source = "dummy"
    version = "0.3.0"  # 0.3.0: leaves, thread broadcasts, me_message, uninterpretable subtypes
    item_source = "slack"  # both dialects simulate the Slack Web API
    dialect = "api"
    archive_backed = False

    def __init__(self, limiter: Limiter) -> None:
        self._limiter = limiter
        self._datasets: dict[str, Dataset] = {}
        self._attempts: dict[
            str, int
        ] = {}  # per request key: failure injection is per process, in order

    # ------------------------------------------------------------------ helpers
    def dataset(self, conn: Connection) -> tuple[Dataset, int]:
        spec_doc = conn.config["spec"]
        key = json.dumps(spec_doc, sort_keys=True, default=str)
        if key not in self._datasets:
            self._datasets[key] = Dataset(DatasetSpec.model_validate(spec_doc))
        return self._datasets[key], int(conn.config.get("epoch", 0))

    def _bucket(self, conn: Connection, method: str) -> BucketKey:
        return BucketKey(conn.tenant_id, self.source, conn.workspace_id, method)

    def _maybe_fail(self, ds: Dataset, request_key: str) -> None:
        f = ds.spec.failures
        total = f.exception_rate + f.timeout_rate + f.throttle_rate
        if total <= 0:
            return
        roll = unit(ds.seed, "fail", f.seed, request_key)
        if roll >= total:
            return
        planned = 1 + h64(ds.seed, "failn", f.seed, request_key) % f.max_consecutive
        done = self._attempts.get(request_key, 0)
        if done >= planned:
            return
        self._attempts[request_key] = done + 1
        if roll < f.exception_rate:
            raise DummySourceError(
                f"injected upstream error for {request_key} (attempt {done + 1}/{planned})"
            )
        if roll < f.exception_rate + f.timeout_rate:
            raise TimeoutError(f"injected timeout for {request_key} (attempt {done + 1}/{planned})")
        raise SourceThrottledError(f.retry_after_seconds)

    @staticmethod
    def _check_auth(conn: Connection) -> None:
        if conn.config.get("auth_revoked"):
            raise AuthenticationError("token_revoked")

    def _conversation_index(self, ds: Dataset, conversation_id: str) -> int:
        return next(i for i, c in enumerate(ds.conversations()) if c.id == conversation_id)

    def _check_available(self, ds: Dataset, conversation_id: str, request_key: str) -> None:
        if (
            self._conversation_index(ds, conversation_id)
            in ds.spec.failures.unavailable_conversations
        ):
            raise DummySourceError(f"injected persistent upstream error for {request_key}")

    async def _request[T](
        self, conn: Connection, ds: Dataset, method: str, request_key: str, respond: Any
    ) -> T:
        async def do() -> T:
            self._check_auth(conn)
            self._maybe_fail(ds, request_key)
            result: T = respond()
            return result

        return await call_with_limits(self._limiter, self._bucket(conn, method), do)

    # ------------------------------------------------------------------ protocol
    async def validate_connection(self, conn: Connection) -> ConnectionInfo:
        ds, _ = self.dataset(conn)
        counts = ds.spec.count_mode is CountMode.TRUE
        blind = (
            () if counts else ("source cannot report message counts: completeness is unverifiable",)
        )
        return ConnectionInfo("dummy", ("history", "replies", "users", "files"), blind, counts)

    async def enumerate(self, conn: Connection, scope: CollectionScope) -> AsyncIterator[WorkUnit]:
        self._check_auth(conn)
        ds, epoch = self.dataset(conn)
        date_from, date_to = ensure_utc(scope.date_from), ensure_utc(scope.date_to)
        if scope.scope_type is ScopeType.CUSTODIAN:
            convs = [c for c in ds.conversations() if scope.external_id in c.members]
        elif scope.external_id == "*":
            convs = list(ds.conversations())
        else:
            convs = [ds.conversation(scope.external_id)]
        for conv in convs:
            for d in range(ds.n_days(epoch)):
                start, end = day_bounds(ds.day(d))
                if start < date_to and end > date_from:
                    yield WorkUnit(conv.id, ds.day(d))

    def _check_access(self, ds: Dataset, epoch: int, conversation_id: str) -> None:
        index = self._conversation_index(ds, conversation_id)
        start = ds.spec.failures.inaccessible_from_epoch.get(index)
        if start is not None and epoch >= start:
            reason = list(AccessLossReason)[index % len(AccessLossReason)]
            body = json.dumps({"ok": False, "error": reason.value}, separators=(",", ":")).encode()
            raise ConversationInaccessibleError(conversation_id, reason, body)

    @staticmethod
    def file_unavailable_reason(
        ds: Dataset, epoch: int, file_ref: str
    ) -> FileUnavailableReason | None:
        f = ds.spec.failures
        if (
            f.file_unavailable_rate <= 0
            or unit(ds.seed, "funavail", f.seed, file_ref) >= f.file_unavailable_rate
        ):
            return None
        reason = DUMMY_FILE_REASONS[
            h64(ds.seed, "freason", f.seed, file_ref) % len(DUMMY_FILE_REASONS)
        ]
        if reason is FileUnavailableReason.EXPIRED_URL and epoch >= 1:
            return None  # transient: a fresh URL works in the next collection
        return reason

    async def item_workspace(self, conn: Connection, conversation_id: str) -> str:
        return conn.workspace_id

    async def expected_count(self, conn: Connection, unit_: WorkUnit) -> int | None:
        self._check_auth(conn)
        ds, epoch = self.dataset(conn)
        self._check_access(ds, epoch, unit_.conversation_id)
        self._check_available(ds, unit_.conversation_id, f"count|{unit_.unit_key}")
        if ds.spec.count_mode is CountMode.UNAVAILABLE:
            return None
        d = ds.day_index(unit_.day)
        count: int = await self._request(
            conn,
            ds,
            "expected_count",
            f"count|{unit_.unit_key}|{epoch}",
            lambda: ds.expected_count(unit_.conversation_id, d, epoch),
        )
        return count  # the TRUE count, even when fetch drops items

    def plan(self, conn: Connection, unit_: WorkUnit, scope: CollectionScope) -> list[_Batch]:
        """The deterministic sequence of requests for one unit (history pages, then thread context)."""
        ds, epoch = self.dataset(conn)
        spec, conv, d = ds.spec, unit_.conversation_id, ds.day_index(unit_.day)
        start, end = day_bounds(unit_.day)
        f = spec.failures
        items = [
            m
            for m in ds.visible_messages(conv, d, epoch)
            if not (f.drop_rate > 0 and unit(ds.seed, "drop", f.seed, conv, m.ts) < f.drop_rate)
        ]
        if spec.messy_pagination:  # out-of-order: deterministic neighbour swaps
            for i in range(len(items) - 1):
                if i == 0 or unit(ds.seed, "swap", conv, d, i) < 0.2:
                    items[i], items[i + 1] = items[i + 1], items[i]
        base = {
            "method": "conversations.history",
            "channel": conv,
            "oldest": f"{int(start.timestamp())}.000000",
            "latest": f"{int(end.timestamp())}.000000",
        }
        batches: list[_Batch] = []
        size, pos, page = spec.page_size, 0, 0
        while pos < len(items) or page == 0:
            if spec.messy_pagination and page == 1 and len(items) > size:
                batches.append(_Batch(BatchKind.HISTORY, (), base))  # an empty page mid-unit
            overlap = (
                1
                if spec.messy_pagination
                and page > 0
                and (page == 2 or unit(ds.seed, "ovl", conv, d, page) < 0.3)
                else 0
            )
            batches.append(
                _Batch(BatchKind.HISTORY, tuple(items[max(0, pos - overlap) : pos + size]), base)
            )
            pos += size
            page += 1
        date_from, date_to = ensure_utc(scope.date_from), ensure_utc(scope.date_to)
        for thread_ts, msgs in ds.thread_context(
            conv, d, epoch, date_from, date_to, scope.thread_parent_policy
        ):
            req = {
                "method": "conversations.replies",
                "channel": conv,
                "ts": thread_ts,
                "policy": scope.thread_parent_policy.value,
            }
            batches.extend(
                _Batch(BatchKind.THREAD_CONTEXT, msgs[offset : offset + size], req)
                for offset in range(0, len(msgs), size)
            )
        return batches

    async def fetch(
        self, conn: Connection, unit_: WorkUnit, cursor: Cursor | None, *, scope: CollectionScope
    ) -> AsyncIterator[RawBatch]:
        ds, epoch = self.dataset(conn)
        self._check_access(ds, epoch, unit_.conversation_id)
        batches = self.plan(conn, unit_, scope)
        index = _decode(cursor)
        if index > len(batches):
            raise InvalidCursorError(f"cursor beyond the end of {unit_.unit_key}")
        corrupt = (
            self._conversation_index(ds, unit_.conversation_id)
            in ds.spec.failures.corrupt_conversations
        )
        while index < len(batches):
            if corrupt and index >= 1:
                raise InvalidCursorError(f"corrupt cursor for {unit_.unit_key} at page {index}")
            self._check_available(ds, unit_.conversation_id, f"fetch|{unit_.unit_key}|{index}")
            batch = batches[index]
            nxt = _encode(index + 1) if index + 1 < len(batches) else None
            more_of_kind = index + 1 < len(batches) and batches[index + 1].kind is batch.kind
            render = slack.history_page if batch.kind is BatchKind.HISTORY else slack.replies_page

            def respond(
                batch: _Batch = batch,
                nxt: Cursor | None = nxt,
                more: bool = more_of_kind,
                render: Any = render,
            ) -> bytes:
                body: bytes = render(
                    ds, list(batch.messages), epoch, has_more=more, next_cursor=nxt
                )
                return body

            body: bytes = await self._request(
                conn,
                ds,
                "fetch",
                f"fetch|{unit_.unit_key}|{epoch}|{scope.thread_parent_policy.value}|{index}",
                respond,
            )
            yield RawBatch(
                body,
                nxt,
                batch.kind,
                {
                    **batch.request,
                    "cursor": _encode(index) if index else "",
                },
            )
            index += 1

    async def fetch_directory(
        self, conn: Connection, cursor: Cursor | None
    ) -> AsyncIterator[RawBatch]:
        """Users, then conversations (with members): one cursor over both, so a resumed directory unit
        continues exactly where it stopped."""
        ds, epoch = self.dataset(conn)
        users = list(ds.users(epoch))
        convs = [c.id for c in ds.conversations()]
        size = ds.spec.page_size
        index = _decode(cursor)
        user_pages = max(1, -(-len(users) // size))
        pages = user_pages + max(1, -(-len(convs) // size))
        while index < pages:
            nxt = _encode(index + 1) if index + 1 < pages else None
            if index < user_pages:
                chunk: list[Any] = users[index * size : (index + 1) * size]
                method, key = "users.list", f"users|{epoch}|{index}"

                def respond(chunk: list[Any] = chunk, nxt: Cursor | None = nxt) -> bytes:
                    return slack.users_page(ds, chunk, epoch, next_cursor=nxt)

            else:
                at = index - user_pages
                chunk = convs[at * size : (at + 1) * size]
                method, key = "conversations.list", f"conversations|{epoch}|{at}"

                def respond(chunk: list[Any] = chunk, nxt: Cursor | None = nxt) -> bytes:
                    return slack.conversations_page(ds, chunk, epoch, next_cursor=nxt)

            body: bytes = await self._request(conn, ds, "directory", key, respond)
            yield RawBatch(
                body,
                nxt,
                BatchKind.DIRECTORY,
                {"method": method, "cursor": _encode(index) if index else ""},
            )
            index += 1

    async def open_file(self, conn: Connection, file_ref: str) -> AsyncIterator[bytes]:
        ds, epoch = self.dataset(conn)
        reason = self.file_unavailable_reason(ds, epoch, file_ref)
        if reason is not None:
            error = {
                "deleted": "file_deleted",
                "external_or_hidden": "file_not_found",
                "expired_url": "url_expired",
                "permission": "access_denied",
            }[reason.value]
            body = json.dumps({"ok": False, "error": error}, separators=(",", ":")).encode()
            raise FileUnavailableError(file_ref, reason, body)
        data: bytes = await self._request(
            conn, ds, "file", f"file|{file_ref}", lambda: ds.file_bytes(file_ref)
        )
        for offset in range(0, max(len(data), 1), 1024):
            yield data[offset : offset + 1024]


def scope_for_days(external_id: str, first: datetime, days: int, **kwargs: Any) -> CollectionScope:
    """Convenience for tests and scripts: a channel scope covering ``days`` whole UTC days."""
    return CollectionScope(
        ScopeType.CHANNEL, external_id, first, first + timedelta(days=days), **kwargs
    )
