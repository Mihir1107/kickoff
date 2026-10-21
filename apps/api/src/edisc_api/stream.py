"""A job's live stream (`GET /v1/jobs/{id}/stream`, M17 step 5, plan §4; ADR 0016 A2.4, A5).

- **One feed per job per API process** (:class:`JobFeed`), polling once per
  ``api_stream_poll_seconds`` while it has subscribers (decision D4): one statement reads the job's
  status and custody head; only when they moved does it read the unit events since its last head and
  the counts. It fans the events out to every subscriber's bounded queue.
- **Each subscriber** subscribes and catches up (unit events after its ``Last-Event-ID``, then a
  snapshot) under the feed's lock, which every poll also holds: the feed has published up to some
  head F, the catch-up reads up to H >= F, and the next poll publishes from F, so nothing between is
  lost; the subscriber drops what it already sent (unit events at or below its last sequence
  number, a snapshot identical to its last). A new feed starts from its first subscriber's H. Ids
  are monotonic: a snapshot's head bounds the unit events sent before it. The poll compares the
  job's status and chain head: a unit's progress that writes no chain event shows in the next
  snapshot taken for a chain or status change.
- **Backpressure:** the producer never waits for a client. A full queue marks the subscriber
  overflowed and drops it from the feed (memory per stream bounded by ``api_stream_queue_events``);
  its writer then sends ``end`` ``slow_consumer`` and closes.
- **Re-checked continuously** (:class:`SessionWatch`, decision D3): at most
  ``api_stream_recheck_seconds`` (<= 15) apart, the session row (revoked, idle or absolute expiry,
  by the database clock), the principal's active flag and ``job.read`` on the job; any failure sends
  ``end`` with the reason and closes. The stream authenticates PASSIVELY (decision D6): it never
  moves the session's activity, so the idle and absolute limits still end it.
- **Never evidence content:** this module reads only the statements in :data:`STATEMENTS` (job
  status, the custody head, the job's connection's status, settled-unit chain events' sequence
  numbers and unit keys, unit rows' status columns, open pauses) and imports nothing that reads
  evidence (both checked by tests).
- **The connection:** its status is part of each poll's compared state and of the snapshot's
  digest; a writer starts from ``active`` and sends ``connection`` before any snapshot whose
  connection status differs from the last one the client knows (``stream_events``).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_api.auth import Caller
from edisc_api.stream_events import (
    MAX_UNITS,
    ConnectionEvent,
    EndEvent,
    EndReason,
    JobEvent,
    ResetEvent,
    ResetReason,
    Strict,
    UnitsEvent,
    UnitState,
)
from edisc_core.logs import get_logger
from edisc_core.schemas import JobStatus
from edisc_db.session import tenant_tx
from edisc_verify.canonical import canonical_json

log = get_logger(__name__)

SETTLED = ("unit_reconciled", "unit_failed")  # chain events that settle a unit
HEAD = (
    "SELECT j.status, j.sealed_at IS NOT NULL AS sealed, j.connection_id,"
    " (SELECT c.status FROM connections c WHERE c.id = j.connection_id) AS connection_status,"
    " (SELECT max(e.seq) FROM custody_events e WHERE e.stream_id = j.id) AS head"
    " FROM collection_jobs j WHERE j.id = :j"
)
SETTLED_SINCE = (  # :limit / :age NULL for a feed's poll (LIMIT NULL = no limit)
    "SELECT e.seq, e.payload->>'unit_key' AS unit_key,"
    " now() - e.created_at > make_interval(secs => :age) AS stale FROM custody_events e"
    " WHERE e.stream_id = :j AND e.seq > :after AND e.seq <= :head"
    " AND e.event_type IN ('unit_reconciled', 'unit_failed') ORDER BY e.seq LIMIT :limit"
)
UNITS = (
    "SELECT unit_key, conversation_id, day, status, recon_status, expected_count,"
    " collected_count, file_gaps FROM work_units WHERE job_id = :j AND unit_key = ANY(:k)"
)
COUNTS = (
    "SELECT status, recon_status, count(*) AS n FROM work_units WHERE job_id = :j"
    " GROUP BY status, recon_status"
)
PAUSED = "SELECT EXISTS (SELECT 1 FROM job_pauses WHERE job_id = :j AND resumed_at IS NULL)"
SESSION = (
    "SELECT s.revoked_at IS NOT NULL AS revoked,"
    " s.idle_expires_at <= now() OR s.absolute_expires_at <= now() AS expired, p.active"
    " FROM sessions s JOIN principals p ON p.id = s.principal_id WHERE s.id = :s"
)
PRINCIPAL = "SELECT active FROM principals WHERE id = :p"
STATEMENTS = (HEAD, SETTLED_SINCE, UNITS, COUNTS, PAUSED, SESSION, PRINCIPAL)
ACTIVE = "active"
CLEAN = {JobStatus.COMPLETED.value: "source", JobStatus.COMPLETED_AGAINST_ARCHIVE.value: "archive"}

Sessions = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Sent:
    """One SSE event as written: its id, name and model."""

    id: str
    name: str
    model: Strict
    seq: int  # the chain position it covers (unit events: their last; snapshots: the head)
    final: bool = False
    connection: ConnectionEvent | None = None  # snapshots: the job's connection at the head

    def wire(self) -> bytes:
        return (
            f"id: {self.id}\nevent: {self.name}\ndata: {self.model.model_dump_json()}\n\n".encode()
        )


def connection_wire(connection: ConnectionEvent) -> bytes:
    """The ``connection`` event that goes before a snapshot: no ``id:``, so the client's
    ``Last-Event-ID`` stays the previous event's until the snapshot arrives."""
    return f"event: connection\ndata: {connection.model_dump_json()}\n\n".encode()


def parse_last_event_id(value: str | None) -> tuple[int, str | None]:
    """``(sequence number, snapshot id)`` from ``Last-Event-ID``; anything else is ignored
    (a fresh start: every settled unit, then the snapshot)."""
    if not value or len(value) > 64:
        return 0, None
    head, _, digest = value.partition(".")
    if not head.isdigit():
        return 0, None
    return int(head), value if digest else None


@dataclass(frozen=True)
class Replay:
    """The bound of a subscriber's catch-up (``EDISC_SSE_REPLAY_MAX``, ``..._MAX_AGE_SECONDS``)."""

    max_events: int
    max_age_seconds: float


async def read_changes(
    s: AsyncSession, job_id: uuid.UUID, after: int, replay: Replay | None = None
) -> tuple[list[Sent], Sent]:
    """Unit events in ``(after, head]`` (chain order, at most MAX_UNITS per event), then the
    snapshot at ``head``. Runs in a REPEATABLE READ transaction (``tenant_tx(...,
    repeatable_read=True)``): every statement sees the database as it was at the head, so the unit
    rows and counts are those of the head, and a snapshot's id names exactly its content. With ``replay`` (a subscriber's catch-up),
    more than ``max_events`` unit events, or (after a ``Last-Event-ID``) an oldest one older than
    ``max_age_seconds``, or a position past the head, gives ONE ``reset`` instead of the units."""
    row = (await s.execute(text(HEAD), {"j": job_id})).one()
    head = int(row.head or 0)
    bound: dict[str, float | None] = {"limit": None, "age": None}
    if replay is not None:  # one row past the cap tells "over" from "exactly at"
        bound = {"limit": replay.max_events + 1,
                 "age": replay.max_age_seconds if after > 0 else None}  # fmt: skip
    settled = (
        await s.execute(text(SETTLED_SINCE), {"j": job_id, "after": after, "head": head, **bound})
    ).all()
    reset: ResetReason | None = None
    if replay is not None:
        if after > head:
            reset = ResetReason.UNKNOWN_POSITION
        elif len(settled) > replay.max_events:
            reset = ResetReason.REPLAY_LIMIT
        elif settled and settled[0].stale:
            reset = ResetReason.REPLAY_AGE
    if reset is not None:
        settled = []
    keys = sorted({r.unit_key for r in settled})
    units = {u.unit_key: u for u in (await s.execute(text(UNITS), {"j": job_id, "k": keys})).all()}
    events: list[Sent] = []
    for start in range(0, len(settled), MAX_UNITS):
        chunk = settled[start : start + MAX_UNITS]
        states = [
            UnitState(
                seq=int(r.seq),
                unit_key=u.unit_key,
                conversation_id=u.conversation_id,
                day=u.day,
                status=u.status,
                recon_status=u.recon_status,
                expected_count=u.expected_count,
                collected_count=u.collected_count,
                file_gaps=u.file_gaps,
            )
            for r in chunk
            if (u := units.get(r.unit_key)) is not None
        ]
        last = int(chunk[-1].seq)
        events.append(Sent(str(last), "units", UnitsEvent(units=states), last))
    by_status: dict[str, int] = {}
    by_recon: dict[str, int] = {}
    for c in (await s.execute(text(COUNTS), {"j": job_id})).all():
        by_status[c.status] = by_status.get(c.status, 0) + int(c.n)
        by_recon[c.recon_status] = by_recon.get(c.recon_status, 0) + int(c.n)
    paused = bool((await s.execute(text(PAUSED), {"j": job_id})).scalar_one())
    snapshot = JobEvent(
        status=row.status, clean=row.status == JobStatus.COMPLETED.value,
        clean_basis=CLEAN.get(row.status), sealed=bool(row.sealed), paused=paused,
        units_by_status=dict(sorted(by_status.items())),
        units_by_recon_status=dict(sorted(by_recon.items())),
    )  # fmt: skip
    connection = ConnectionEvent(
        connection_id=row.connection_id, status=row.connection_status,
        needs_reauthorization=row.connection_status == "reauth_required",
    )  # fmt: skip
    digest = hashlib.sha256(
        canonical_json({"job": snapshot.model_dump(mode="json"),
                        "connection": connection.model_dump(mode="json")})
    ).hexdigest()[:12]  # fmt: skip
    if reset is not None:
        events = [Sent(str(head), "reset", ResetEvent(reason=reset), head)]
    return events, Sent(f"{head}.{digest}", "job", snapshot, head, final=bool(row.sealed),
                        connection=connection)  # fmt: skip


# ------------------------------------------------------------------ fan-out
@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue[Sent]
    overflowed: bool = False


@dataclass(eq=False)
class JobFeed:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    sessions: Sessions
    poll_seconds: float
    subscribers: set[Subscriber] = field(default_factory=set)
    # (status, sealed, head, connection status) of the last poll: sealing moves neither the status
    # nor the chain head; a connection's status moves neither either
    last: tuple[str, bool, int, str | None] | None = None
    after: int | None = None  # the head the feed has published up to
    task: asyncio.Task[None] | None = None
    polls: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def subscribe(self, size: int) -> Subscriber:
        sub = Subscriber(asyncio.Queue(maxsize=size))
        self.subscribers.add(sub)
        return sub

    def publish(self, events: list[Sent]) -> None:
        for sub in list(self.subscribers):
            for event in events:
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    # never wait for a client: drop it (its writer ends it with slow_consumer)
                    sub.overflowed = True
                    self.subscribers.discard(sub)
                    break

    async def join(
        self, size: int, after: int, replay: Replay
    ) -> tuple[Subscriber, list[Sent], Sent]:
        """Subscribe and catch up atomically with respect to polls (the module docstring)."""
        async with self.lock:
            sub = self.subscribe(size)
            try:
                # one snapshot of the database: the head bounds everything read with it
                async with tenant_tx(self.sessions, self.tenant_id, repeatable_read=True) as s:
                    events, snapshot = await read_changes(s, self.job_id, after, replay)
            except BaseException:  # never leave a subscriber that no writer will drain
                self.subscribers.discard(sub)
                raise
            if self.after is None:
                self.after = snapshot.seq
                model, conn = snapshot.model, snapshot.connection
                self.last = (model.status, model.sealed, snapshot.seq,  # type: ignore[attr-defined]
                             conn.status if conn is not None else None)  # fmt: skip
            return sub, events, snapshot

    async def poll_once(self) -> None:
        self.polls += 1
        async with self.lock:
            # one snapshot: the counts never include a unit settled after the head was read
            # (found by CI run 38897021941: a "head 5" snapshot showing unit 6, sent before it)
            async with tenant_tx(self.sessions, self.tenant_id, repeatable_read=True) as s:
                row = (await s.execute(text(HEAD), {"j": self.job_id})).one()
                state = (row.status, bool(row.sealed), int(row.head or 0), row.connection_status)
                if state == self.last or self.after is None:
                    return
                events, snapshot = await read_changes(s, self.job_id, self.after)
            self.last, self.after = state, snapshot.seq
            self.publish([*events, snapshot])

    async def run(self, on_idle: Callable[[], None]) -> None:
        try:
            while self.subscribers:
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001 -- one failed poll: the next one retries
                    # (a database blip must not end every stream; subscribers re-check on their own)
                    log.warning("job stream poll failed", error=type(exc).__name__)
                await asyncio.sleep(self.poll_seconds)
        finally:
            on_idle()


@dataclass
class JobFeeds:
    """The process's feeds, one per job, started on the first subscriber, stopped after the last."""

    sessions: Sessions
    poll_seconds: float
    feeds: dict[uuid.UUID, JobFeed] = field(default_factory=dict)
    per_principal: dict[uuid.UUID, int] = field(default_factory=dict)

    def feed(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> JobFeed:
        feed = self.feeds.get(job_id)
        if feed is None:
            feed = self.feeds[job_id] = JobFeed(tenant_id, job_id, self.sessions, self.poll_seconds)
        return feed

    def release(self, feed: JobFeed) -> None:
        """Forget a feed nobody subscribes to and nothing runs for (a stream that ended before its
        feed started; a running feed removes itself when its last subscriber leaves)."""
        idle = not feed.subscribers and (feed.task is None or feed.task.done())
        if idle and self.feeds.get(feed.job_id) is feed:
            del self.feeds[feed.job_id]

    def start(self, feed: JobFeed) -> None:
        if feed.task is None or feed.task.done():

            def idle() -> None:
                if self.feeds.get(feed.job_id) is feed and not feed.subscribers:
                    del self.feeds[feed.job_id]

            feed.task = asyncio.get_running_loop().create_task(feed.run(idle))


# ------------------------------------------------------------------ continuous authorization
@dataclass
class SessionWatch:
    """The session row (cookie callers) or the principal's active flag (bearer callers), checked by
    the DATABASE clock, shared by all the streams and downloads of one session.

    The bound ``seconds`` (D3: <= 15) is the SUM of two halves: a stream or download calls
    :meth:`check` every :attr:`interval` (``seconds / 2``), and a cached answer is at most
    ``interval`` old. Caching for the whole bound while also checking every ``seconds`` let a second
    stream of one session use an answer read just before a revocation, then wait a full period:
    up to twice the bound (found while building 5b; ``test_two_streams_of_one_session...``)."""

    sessions: Sessions
    seconds: float
    cache: dict[uuid.UUID, tuple[float, EndReason | None]] = field(default_factory=dict)

    @property
    def interval(self) -> float:
        return self.seconds / 2

    async def check(self, caller: Caller) -> EndReason | None:
        key = caller.session_id or caller.principal_id
        now = time.monotonic()
        hit = self.cache.get(key)
        if hit is not None and now - hit[0] < self.interval:
            return hit[1]
        async with tenant_tx(self.sessions, caller.tenant_id) as s:
            if caller.session_id is not None:
                row = (await s.execute(text(SESSION), {"s": caller.session_id})).one_or_none()
                reason = (
                    EndReason.SESSION_REVOKED if row is None or row.revoked
                    else EndReason.SESSION_EXPIRED if row.expired
                    else None if row.active else EndReason.PRINCIPAL_DEACTIVATED
                )  # fmt: skip
            else:
                active = (await s.execute(text(PRINCIPAL), {"p": caller.principal_id})).scalar()
                reason = None if active else EndReason.PRINCIPAL_DEACTIVATED
        self.cache[key] = (now, reason)
        return reason


# ------------------------------------------------------------------ one stream
Authorized = Callable[[], Awaitable[bool]]


async def shielded[T](work: Awaitable[T], undo: Callable[[T], None] | None = None) -> T:
    """Database work of a stream, shielded from the client's disconnect: cancelling the response
    mid-transaction would leave a pooled connection unreturned (fixed pools: lost capacity). If the
    stream is cancelled meanwhile, the work still completes and ``undo`` reverses its effect."""
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if undo is not None:
            task.add_done_callback(
                lambda t: undo(t.result()) if not t.cancelled() and t.exception() is None else None
            )
        raise


async def stream_body(
    *, feeds: JobFeeds, watch: SessionWatch, caller: Caller, job_id: uuid.UUID,
    last_event_id: str | None, authorized: Authorized, queue_size: int, recheck_seconds: float,
    max_seconds: float, replay: Replay, on_close: Callable[[], None],
) -> AsyncIterator[bytes]:  # fmt: skip
    feed = feeds.feed(caller.tenant_id, job_id)
    started = time.monotonic()
    last_seq, last_snapshot = parse_last_event_id(last_event_id)
    # the connection status this client knows: a stream starts from `active` (the normal state,
    # nothing to say), so `connection` goes out when the connection is anything else at the start,
    # and on every change after
    last_connection = ACTIVE
    sub: Subscriber | None = None

    def with_connection(event: Sent) -> bytes:
        """A snapshot, preceded by ``connection`` when the client has not had that status."""
        nonlocal last_connection
        out = event.wire()
        if event.connection is not None and event.connection.status != last_connection:
            out = connection_wire(event.connection) + out
            last_connection = event.connection.status
        return out

    def fresh(event: Sent) -> Sent | None:
        """The part of ``event`` this client has not had: a feed's event may overlap its catch-up
        (it covers chain events since the FEED's last head), so units are trimmed one by one."""
        if event.name == "reset":
            return event
        if event.name == "units":
            kept = [u for u in event.model.units if u.seq > last_seq]  # type: ignore[attr-defined]
            if not kept:
                return None
            return Sent(str(kept[-1].seq), "units", UnitsEvent(units=kept), kept[-1].seq)
        return event if event.id != last_snapshot and event.seq >= last_seq else None

    def end(reason: EndReason) -> bytes:
        return f"event: end\ndata: {EndEvent(reason=reason).model_dump_json()}\n\n".encode()

    try:
        yield b"retry: 3000\n\n"
        joined, events, snapshot = await shielded(
            feed.join(queue_size, last_seq, replay), lambda done: _leave(feeds, feed, done[0])
        )
        sub = joined
        feeds.start(feed)
        if snapshot.id == last_snapshot and snapshot.connection is not None:
            last_connection = snapshot.connection.status  # its digest covers the connection
        for caught in [*events, snapshot]:
            event = fresh(caught)
            if event is not None:
                yield with_connection(event)
                if event.name == "reset":  # the client reloads: positions restart at the head
                    last_seq, last_snapshot = event.seq, None
                    continue
                last_seq = max(last_seq, event.seq)
                if event.name == "job":
                    last_snapshot = event.id
        if snapshot.final:
            yield end(EndReason.SEALED)
            return
        # the session every watch.interval (its cache is at most as old: <= recheck_seconds in
        # all, SessionWatch), job.read every recheck_seconds (never cached)
        checked = permitted = time.monotonic()
        while True:
            if joined.overflowed:
                yield end(EndReason.SLOW_CONSUMER)
                return
            wait = max(0.0, watch.interval - (time.monotonic() - checked))
            live: Sent | None
            try:
                live = await asyncio.wait_for(joined.queue.get(), timeout=wait)
            except TimeoutError:
                live = None
            live = fresh(live) if live is not None else None
            if live is not None:
                yield with_connection(live)
                last_seq = max(last_seq, live.seq)
                if live.name == "job":
                    last_snapshot = live.id
                if live.final:
                    yield end(EndReason.SEALED)
                    return
            if time.monotonic() - checked >= watch.interval:
                reason = await shielded(watch.check(caller))
                if reason is None and time.monotonic() - permitted >= recheck_seconds:
                    if not await shielded(authorized()):
                        reason = EndReason.PERMISSION_LOST
                    permitted = time.monotonic()
                if reason is None and time.monotonic() - started >= max_seconds:
                    reason = EndReason.LIFETIME
                if reason is not None:
                    yield end(reason)
                    return
                checked = time.monotonic()
                yield b": keepalive\n\n"
    finally:
        if sub is not None:
            feed.subscribers.discard(sub)
        feeds.release(feed)
        on_close()


def _leave(feeds: JobFeeds, feed: JobFeed, sub: Subscriber) -> None:
    feed.subscribers.discard(sub)
    feeds.release(feed)
