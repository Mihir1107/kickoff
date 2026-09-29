"""UTC time utilities. Naive datetimes are always rejected, never assumed to be UTC."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from pydantic import AfterValidator


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


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_utc(text: str) -> datetime:
    """Parse an ISO 8601 / RFC 3339 timestamp that carries an explicit offset (``Z`` allowed)."""
    return ensure_utc(datetime.fromisoformat(text))


def from_epoch(seconds: float | str) -> datetime:
    """Convert epoch seconds (e.g. Slack ``ts`` strings) to an aware UTC datetime."""
    if isinstance(seconds, str):
        whole, _, frac = seconds.partition(".")
        micros = int((frac + "000000")[:6]) if frac else 0
        return datetime.fromtimestamp(int(whole), UTC) + timedelta(microseconds=micros)
    return datetime.fromtimestamp(seconds, UTC)


def format_utc(value: datetime) -> str:
    """Stable RFC 3339 form used in hashes and payloads: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_day(value: datetime) -> date:
    """The UTC calendar day an instant falls on (unit-of-work day, ADR 0005)."""
    return ensure_utc(value).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` UTC interval covering ``day``."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
"""Pydantic field type: accepts only offset-aware datetimes and stores them in UTC."""
