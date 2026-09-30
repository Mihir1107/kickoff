"""UTC time utilities. Naive datetimes are always rejected, never assumed to be UTC."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
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
    """Half-open ``[start, end)`` UTC interval covering the UTC calendar ``day`` (the default)."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


_ONE_MICRO = timedelta(microseconds=1)


def _wall(instant_utc: datetime, zone: tzinfo) -> datetime:
    return instant_utc.astimezone(zone).replace(tzinfo=None, fold=0)


def resolve_wall_time(wall: datetime, zone: tzinfo) -> datetime:
    """Map a local wall-clock time in ``zone`` to a UTC instant, deterministically, never raising.

    Used for *boundaries* (e.g. local-midnight day slicing), not for parsing inputs:

    - existing time: its instant (for an ambiguous time, the earlier occurrence, ``fold=0``);
    - nonexistent time (DST gap, or a skipped day): the earliest valid instant whose local wall time
      is at or after ``wall``, i.e. the transition instant that ends the gap.
    """
    if wall.tzinfo is not None:
        raise TypeError("resolve_wall_time takes a wall-clock (naive) time plus an explicit zone")
    first = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if _wall(first, zone) == wall:
        return first
    # In a gap, fold=0/1 interpret ``wall`` with the offsets before/after the transition. The transition
    # instant lies between them, and local time is monotonic there. Binary search in microseconds for
    # the first instant whose wall time is >= ``wall``.
    second = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    lo, hi = min(first, second), max(first, second)
    while _wall(hi, zone) < wall:  # defensive: widen until the upper bound is past the gap
        hi += timedelta(hours=1)
    while hi - lo > _ONE_MICRO:
        mid = lo + (hi - lo) // 2
        if _wall(mid, zone) >= wall:
            hi = mid
        else:
            lo = mid
    return lo if _wall(lo, zone) >= wall else hi


def local_day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """Half-open UTC interval for the local calendar ``day`` in ``zone``.

    Boundaries are resolved with :func:`resolve_wall_time`, so days starting inside a DST gap begin
    at the first valid instant after local midnight; a day skipped entirely (e.g. Pacific/Apia
    2011-12-30) yields an empty interval. Output days may be 23h, 25h, 23.5h, 0h, and so on.
    """
    start = resolve_wall_time(datetime.combine(day, time.min), zone)
    end = resolve_wall_time(datetime.combine(day + timedelta(days=1), time.min), zone)
    return start, end


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
"""Pydantic field type: accepts only offset-aware datetimes and stores them in UTC."""
