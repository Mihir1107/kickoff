"""The UTC rules the verifier needs (the product's `edisc_core.time` re-exports them): naive
datetimes are always rejected, never assumed to be UTC."""

from __future__ import annotations

from datetime import UTC, datetime


class NaiveDatetimeError(ValueError):
    """Raised when a datetime without tzinfo reaches code that needs an instant in time."""


class NonexistentLocalTimeError(ValueError):
    """Raised for a wall-clock time that never happened in its zone (DST gap)."""


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` converted to UTC. Raises ``NaiveDatetimeError`` if it has no offset."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(f"naive datetime rejected: {value.isoformat()}")
    converted = value.astimezone(UTC)
    if converted.astimezone(value.tzinfo).replace(tzinfo=None, fold=0) != value.replace(
        tzinfo=None, fold=0
    ):
        raise NonexistentLocalTimeError(f"local time does not exist in its zone: {value!r}")
    return converted


def format_utc(value: datetime) -> str:
    """Stable RFC 3339 form used in hashes and payloads: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
