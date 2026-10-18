"""Directory snapshots for the scope picker (M17 plan §3.3, migration 0043).

``refresh-directories`` (a maintenance sweep, and one run started after a connection is created
or re-authorized) captures each due connection's directory into a NEW generation
(``directory_snapshots``), page by page, never on a request:

- **The source** is the connector's ``fetch_directory`` (users, then conversations, one resumable
  cursor; every request through the rate limiter). An export (``slack_export``) makes no source
  call: its conversations come from the validated ``export_conversations`` rows and its users from
  the locked archive's ``users.json``.
- **Each page in one transaction**: the generation's row is locked, its checkpoint
  (``source_cursor``, the cursor of the page being stored) must still be the one this executor
  read (another executor that moved it wins; this one reloads), the page's entries are inserted
  (one row per kind and external id: a page stored twice adds nothing), and the checkpoint moves
  to the next page's cursor with the counts. The last page completes the generation in the same
  transaction, and generations older than the current and the previous one become ``superseded``.
  A SIGKILL anywhere resumes from the last committed page; a generation is never served before it
  is complete.
- **Bounded runs:** a run stops at a page boundary after ``directory_refresh_budget_seconds``; the
  next run resumes the generation. Grid-sized directories take several runs.
- **Failures:** refused credentials or a page the normalizer cannot read end the generation
  ``failed`` (never served; the previous complete one stays current); any other error leaves it
  ``capturing`` to resume, is collected, and fails the run after the other connections (retried by
  the schedule). A source without a connector in this worker is counted ``unchecked``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_connectors_base.protocol import Connector
from edisc_connectors_base.types import AuthenticationError, Connection
from edisc_core.ids import new_id
from edisc_core.logs import get_logger
from edisc_core.lone_surrogates import recorded_text
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_normalizer import directory
from edisc_normalizer.slack import NormalizationError
from edisc_worker.pipeline import CrashHooks

log = get_logger(__name__)

EXPORT_SOURCE = "slack_export"
# the connector's directory requests: which kind of entries a page holds
METHOD_KINDS = {
    "users.list": directory.USER, "conversations.list": directory.CONVERSATION,
    "export.users": directory.USER,
}  # fmt: skip
Sessions = async_sessionmaker[AsyncSession]


@dataclass
class DirectoriesRefreshed:
    completed: int = 0
    partial: int = 0  # the budget ended the run first: the next run resumes
    failed: int = 0
    pages: int = 0
    unchecked: dict[str, int] = field(default_factory=dict)  # source -> no connector here
    errors: list[str] = field(default_factory=list)


@dataclass
class _Open:
    id: uuid.UUID
    generation: int
    cursor: str | None
    pages: int


def parse_page(body: bytes, method: str, dialect: str) -> list[directory.DirectoryEntry]:
    """One page's entries: CPU work, run in a thread (a Grid users page is large)."""
    kind = METHOD_KINDS.get(method)
    if kind is None:
        raise NormalizationError(f"not a directory page: {method}")
    return directory.entries(body, kind=kind, dialect=dialect)


async def refresh_directories(
    sweeper_sessions: Sessions, sessions: Sessions, settings: Settings,
    connectors: dict[str, Connector], *, tenant_id: uuid.UUID | None = None,
    hooks: CrashHooks | None = None, limit: int = 500,
) -> DirectoriesRefreshed:  # fmt: skip
    deadline = time.monotonic() + settings.directory_refresh_budget_seconds
    async with sweeper_sessions() as s, s.begin():
        due = (
            await s.execute(
                text("SELECT tenant_id, connection_id, source FROM directories_due(:age, :n)"),
                {"age": settings.directory_refresh_max_age_hours * 3600, "n": limit},
            )
        ).all()
    out = DirectoriesRefreshed()
    for r in due:
        if tenant_id is not None and r.tenant_id != tenant_id:
            continue
        connector = connectors.get(r.source)
        if connector is None:
            out.unchecked[r.source] = out.unchecked.get(r.source, 0) + 1
            continue
        if time.monotonic() >= deadline:
            out.partial += 1
            continue
        try:
            outcome, pages = await refresh_one(
                sessions, connector, tenant_id=r.tenant_id, connection_id=r.connection_id,
                deadline=deadline, hooks=hooks or CrashHooks(),
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 -- collected: the run fails after the others
            # the type only: a source's error text may quote its answer
            log.error("directory refresh failed", connection_id=str(r.connection_id),  # noqa: TRY400
                      error=type(exc).__name__)  # fmt: skip
            out.errors.append(f"{r.connection_id}: {type(exc).__name__}")
            continue
        out.pages += pages
        setattr(out, outcome, getattr(out, outcome) + 1)
    if out.unchecked:
        log.warning("directories not refreshable by this worker", unchecked=out.unchecked)
    if out.errors:
        raise RuntimeError(f"directory refresh failed for {len(out.errors)} connection(s)")
    return out


async def refresh_one(
    sessions: Sessions, connector: Connector, *, tenant_id: uuid.UUID, connection_id: uuid.UUID,
    deadline: float, hooks: CrashHooks,
) -> tuple[str, int]:  # fmt: skip
    """Capture (or resume) the connection's capturing generation. Returns ``(outcome, pages)``
    with outcome ``completed``, ``partial`` or ``failed``."""
    conn, snap = await _open(sessions, connector, tenant_id, connection_id)
    if snap is None:
        return "completed", 0  # the connection is no longer active
    if conn.source == EXPORT_SOURCE and snap.pages == 0:
        await _export_conversations(sessions, tenant_id, snap, conn)
    pages = 0
    while True:
        moved = False
        source = connector.fetch_directory(conn, snap.cursor)
        try:
            async for batch in source:
                method = batch.request.get("method", "")
                found = await asyncio.to_thread(parse_page, batch.body, method, connector.dialect)
                stored = await _store_page(
                    sessions, tenant_id, snap, found, batch.next_cursor, hooks
                )
                if stored is None:  # another executor moved the checkpoint: reload, continue
                    moved = True
                    break
                pages += 1
                await hooks.hit("directory_after_page")
                if batch.next_cursor is None:
                    return "completed", pages
                snap.cursor, snap.pages = batch.next_cursor, snap.pages + 1
                if time.monotonic() >= deadline:
                    return "partial", pages
        except (AuthenticationError, NormalizationError) as exc:
            await _fail(sessions, tenant_id, snap, exc)
            return "failed", pages
        finally:
            await source.aclose()  # type: ignore[attr-defined]
        if not moved:  # the source ended without a last page (an empty directory)
            await _store_page(sessions, tenant_id, snap, [], None, hooks)
            return "completed", pages
        reloaded = await _reload(sessions, tenant_id, snap.id)
        if reloaded is None:
            return "completed", pages  # the other executor finished it
        snap = reloaded


async def _open(
    sessions: Sessions, connector: Connector, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[Connection, _Open | None]:
    async with tenant_tx(sessions, tenant_id) as s:
        c = (
            await s.execute(
                text(
                    "SELECT id, source, external_org_id, config, status FROM connections"
                    " WHERE id = :c FOR NO KEY UPDATE"
                ),
                {"c": connection_id},
            )
        ).one()
        conn = Connection(tenant_id, c.id, c.source, c.external_org_id, dict(c.config))
        if c.status != "active":
            return conn, None
        row = (
            await s.execute(
                text(
                    "SELECT id, generation, source_cursor, pages FROM directory_snapshots"
                    " WHERE connection_id = :c AND status = 'capturing'"
                ),
                {"c": connection_id},
            )
        ).one_or_none()
        if row is not None:
            return conn, _Open(row.id, row.generation, row.source_cursor, row.pages)
        generation = int(
            (
                await s.execute(
                    text(
                        "SELECT coalesce(max(generation), 0) + 1 FROM directory_snapshots"
                        " WHERE connection_id = :c"
                    ),
                    {"c": connection_id},
                )
            ).scalar_one()
        )
        snap = _Open(new_id(), generation, None, 0)
        await s.execute(
            text(
                "INSERT INTO directory_snapshots (id, tenant_id, connection_id, generation,"
                " connector_version) VALUES (:i, :t, :c, :g, :v)"
            ),
            {"i": snap.id, "t": tenant_id, "c": connection_id, "g": generation,
             "v": connector.version},
        )  # fmt: skip
    log.info("directory capture started", connection_id=str(connection_id), generation=generation)
    return conn, snap


async def _reload(sessions: Sessions, tenant_id: uuid.UUID, snapshot_id: uuid.UUID) -> _Open | None:
    async with tenant_tx(sessions, tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT id, generation, source_cursor, pages, status FROM directory_snapshots"
                    " WHERE id = :i"
                ),
                {"i": snapshot_id},
            )
        ).one()
    if row.status != "capturing":
        return None
    return _Open(row.id, row.generation, row.source_cursor, row.pages)


def _rows(tenant_id: uuid.UUID, snapshot_id: uuid.UUID,
          found: Sequence[directory.DirectoryEntry]) -> dict[str, Any]:  # fmt: skip
    return {
        "t": tenant_id, "s": snapshot_id,
        "kind": [e.kind for e in found], "id": [e.external_id for e in found],
        "name": [e.name for e in found],
        # a text[] of JSON arrays: unnest cannot take a ragged integer[][]
        "units": [None if e.name_units is None else str(e.name_units) for e in found],
        "type": [e.entry_type for e in found], "archived": [e.archived for e in found],
        "key": [e.search_key for e in found],
    }  # fmt: skip


INSERT_ENTRIES = (
    "INSERT INTO directory_entries (tenant_id, snapshot_id, kind, external_id, name, name_units,"
    " entry_type, archived, search_key) SELECT CAST(:t AS uuid), CAST(:s AS uuid), k, i, n,"
    " CASE WHEN u IS NULL THEN NULL ELSE ARRAY(SELECT jsonb_array_elements_text(u::jsonb)::int)"
    " END, ty, a, sk FROM unnest(CAST(:kind AS text[]), CAST(:id AS text[]), CAST(:name AS text[]),"
    " CAST(:units AS text[]), CAST(:type AS text[]), CAST(:archived AS boolean[]),"
    " CAST(:key AS text[])) AS x(k, i, n, u, ty, a, sk)"
    " ON CONFLICT (snapshot_id, kind, external_id) DO NOTHING RETURNING kind"
)


async def _store_page(
    sessions: Sessions, tenant_id: uuid.UUID, snap: _Open,
    found: Sequence[directory.DirectoryEntry], next_cursor: str | None,
    hooks: CrashHooks | None = None,
) -> int | None:  # fmt: skip
    """One page in one transaction (the module docstring). Returns the entries added, or None when
    the checkpoint is no longer the one this executor read."""
    async with tenant_tx(sessions, tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, source_cursor, pages, connection_id FROM directory_snapshots"
                    " WHERE id = :i FOR NO KEY UPDATE"
                ),
                {"i": snap.id},
            )
        ).one()
        if row.status != "capturing" or row.source_cursor != snap.cursor or row.pages != snap.pages:
            return None
        added: Counter[str] = Counter()
        if found:
            kinds = await s.execute(text(INSERT_ENTRIES), _rows(tenant_id, snap.id, found))
            added.update(kinds.scalars())
        if hooks is not None:
            await hooks.hit("directory_page_tx")  # entries written, checkpoint not yet moved
        done = next_cursor is None
        await s.execute(
            text(
                "UPDATE directory_snapshots SET source_cursor = :next, pages = pages + 1,"
                " conversations = conversations + :c, users = users + :u, updated_at = now(),"
                " status = CASE WHEN :done THEN 'complete' ELSE status END,"
                " completed_at = CASE WHEN :done THEN now() END WHERE id = :i"
            ),
            {"next": next_cursor, "c": added[directory.CONVERSATION], "u": added[directory.USER],
             "done": done, "i": snap.id},
        )  # fmt: skip
        if done:
            # keep the current and the previous complete generation (paging continues in either)
            await s.execute(
                text(
                    "UPDATE directory_snapshots SET status = 'superseded', updated_at = now()"
                    " WHERE connection_id = :c AND status = 'complete' AND generation < ("
                    " SELECT max(generation) FROM directory_snapshots WHERE connection_id = :c"
                    " AND status = 'complete' AND generation < :g)"
                ),
                {"c": row.connection_id, "g": snap.generation},
            )
    if next_cursor is None:
        log.info("directory captured", snapshot_id=str(snap.id), generation=snap.generation)
    return sum(added.values())


async def _fail(sessions: Sessions, tenant_id: uuid.UUID, snap: _Open, exc: Exception) -> None:
    reason, _ = recorded_text(f"{type(exc).__name__}: {exc}", 2000)
    async with tenant_tx(sessions, tenant_id) as s:
        await s.execute(
            text(
                "UPDATE directory_snapshots SET status = 'failed', failed_reason = :r,"
                " updated_at = now() WHERE id = :i AND status = 'capturing'"
            ),
            {"r": reason, "i": snap.id},
        )
    log.warning("directory capture failed", snapshot_id=str(snap.id), error=type(exc).__name__)


async def _export_conversations(
    sessions: Sessions, tenant_id: uuid.UUID, snap: _Open, conn: Connection
) -> None:
    """An export's conversations, from its validated rows (no source call), before its users
    (the connector's ``users.json`` page, which completes the generation)."""
    export_id = uuid.UUID(str(conn.config["export_id"]))
    async with tenant_tx(sessions, tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT conversation_id, kind, name, exact FROM export_conversations"
                    ' WHERE export_id = :e ORDER BY conversation_id COLLATE "C"'
                ),
                {"e": export_id},
            )
        ).all()
        found = [
            directory.export_conversation_entry(
                r.conversation_id, r.name, r.kind, (r.exact or {}).get("/name")
            )
            for r in rows
        ]
        if found:
            kinds = await s.execute(text(INSERT_ENTRIES), _rows(tenant_id, snap.id, found))
            added = sum(1 for _ in kinds.scalars())
            await s.execute(
                text(
                    "UPDATE directory_snapshots SET conversations = conversations + :c,"
                    " updated_at = now() WHERE id = :i AND status = 'capturing' AND pages = 0"
                ),
                {"c": added, "i": snap.id},
            )
