"""The connector interface. Everything a connector yields is raw; the normalizer interprets it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from edisc_connectors_base.ratelimit import BucketKey, Grant, WaitCallback
from edisc_connectors_base.types import (
    CollectionScope,
    Connection,
    ConnectionInfo,
    Cursor,
    RawBatch,
    WorkUnit,
)


class Limiter(Protocol):
    """The rate-limit hook (implemented by ``RateLimiter``). Every source request takes a token first."""

    async def acquire(self, key: BucketKey, *, on_wait: WaitCallback | None = None) -> Grant: ...

    async def pause(self, key: BucketKey, retry_after_seconds: float) -> int | None: ...


class Connector(Protocol):
    source: str  # the connector (dummy, slack, slack_export, ...)
    version: str  # semver, recorded on every item and custody event
    # the identity namespace of what it collects: the same Slack message gets the same idempotency key
    # whichever connector (live API, export) collected it (ADR 0004, ADR 0014 section 7)
    item_source: str
    dialect: str  # normalizer dialect: "api" pages or "export" files
    archive_backed: bool  # units are export day files: reconciled against the archive (ADR 0014)

    async def validate_connection(self, conn: Connection) -> ConnectionInfo:
        """Plan tier, granted scopes, known blind spots, whether the source can report counts."""
        ...

    def enumerate(self, conn: Connection, scope: CollectionScope) -> AsyncIterator[WorkUnit]:
        """Units of work = (conversation_id, UTC day) inside the scope."""
        ...

    async def item_workspace(self, conn: Connection, conversation_id: str) -> str:
        """The workspace namespacing this conversation's item identities (ADR 0004): the connection's
        workspace, or the conversation's own team where one connection spans several (Grid exports)."""
        ...

    async def expected_count(self, conn: Connection, unit: WorkUnit) -> int | None:
        """Distinct messages the SOURCE says exist in the unit; None if it cannot say (reported as such)."""
        ...

    def fetch(
        self, conn: Connection, unit: WorkUnit, cursor: Cursor | None, *, scope: CollectionScope
    ) -> AsyncIterator[RawBatch]:
        """Raw pages from ``cursor`` onward (None = start). ``scope`` carries the date range and
        thread-parent policy that decide which out-of-range thread context is fetched."""
        ...

    def fetch_directory(self, conn: Connection, cursor: Cursor | None) -> AsyncIterator[RawBatch]:
        """Raw user/identity pages (for identity snapshots)."""
        ...

    def open_file(self, conn: Connection, file_ref: str) -> AsyncIterator[bytes]:
        """Stream one attachment's bytes (stored as its own evidence object)."""
        ...
