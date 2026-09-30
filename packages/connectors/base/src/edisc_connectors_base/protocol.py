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
    source: str
    version: str  # semver, recorded on every item and custody event

    async def validate_connection(self, conn: Connection) -> ConnectionInfo:
        """Plan tier, granted scopes, known blind spots, whether the source can report counts."""
        ...

    def enumerate(self, conn: Connection, scope: CollectionScope) -> AsyncIterator[WorkUnit]:
        """Units of work = (conversation_id, UTC day) inside the scope."""
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
