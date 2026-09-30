"""Local-day boundaries (RSMF slicing in a matter/custodian timezone): DST gaps resolve deterministically
to the earliest valid instant at or after local midnight, never raise. UTC stays the default."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edisc_core.time import day_bounds, local_day_bounds, resolve_wall_time

# Zones whose DST transitions happen at (or have happened at) local midnight, plus odd ones:
# 30-minute DST (Lord_Howe), negative DST (Dublin), a skipped calendar day (Apia 2011-12-30).
MIDNIGHT_ZONES = [
    "America/Santiago",
    "America/Asuncion",
    "America/Havana",
    "America/Sao_Paulo",
    "Asia/Beirut",
    "Asia/Damascus",
    "Asia/Amman",
    "Asia/Tehran",
    "Africa/Cairo",
    "Africa/Casablanca",
    "America/Scoresbysund",
    "Pacific/Apia",
    "Australia/Lord_Howe",
    "Europe/Dublin",
    "America/New_York",
]


def _wall(instant: datetime, zone: ZoneInfo) -> datetime:
    return instant.astimezone(zone).replace(tzinfo=None, fold=0)


@settings(max_examples=3000, deadline=None)
@given(
    st.sampled_from(MIDNIGHT_ZONES),
    st.dates(min_value=date(1975, 1, 1), max_value=date(2037, 12, 30)),
)
def test_local_day_bounds_properties(zone_name: str, day: date) -> None:
    zone = ZoneInfo(zone_name)
    start, end = local_day_bounds(day, zone)
    midnight = datetime.combine(day, time.min)
    assert start.tzinfo is UTC
    assert end.tzinfo is UTC
    # earliest valid instant at or after local midnight
    assert _wall(start, zone) >= midnight
    assert _wall(start - timedelta(microseconds=1), zone) < midnight
    # contiguous, non-overlapping, never negative, never absurd
    assert end == local_day_bounds(day + timedelta(days=1), zone)[0]
    assert timedelta(0) <= end - start <= timedelta(hours=26)
    # deterministic
    assert local_day_bounds(day, zone) == (start, end)


@settings(max_examples=500, deadline=None)
@given(
    st.sampled_from(MIDNIGHT_ZONES),
    st.datetimes(min_value=datetime(1975, 1, 1), max_value=datetime(2037, 12, 30)),  # noqa: DTZ001 - wall clock
)
def test_resolve_wall_time_never_raises_and_is_earliest(zone_name: str, wall: datetime) -> None:
    zone = ZoneInfo(zone_name)
    instant = resolve_wall_time(wall, zone)
    assert _wall(instant, zone) >= wall
    assert _wall(instant - timedelta(microseconds=1), zone) < wall


@pytest.mark.parametrize(
    ("zone", "day", "expected_start_utc"),
    [
        # Brazil 2018: clocks jumped 00:00 -> 01:00 (-03 -> -02); day starts 01:00 local = 03:00Z
        ("America/Sao_Paulo", date(2018, 11, 4), datetime(2018, 11, 4, 3, 0, tzinfo=UTC)),
        # Chile 2022: 24:00 Sep 10 -> 01:00 Sep 11 (-04 -> -03); day starts 01:00 local = 04:00Z
        ("America/Santiago", date(2022, 9, 11), datetime(2022, 9, 11, 4, 0, tzinfo=UTC)),
        # Cuba 2022: 01:00 -> 00:00 (-04 -> -05), midnight occurs twice; earliest is 04:00Z
        ("America/Havana", date(2022, 11, 6), datetime(2022, 11, 6, 4, 0, tzinfo=UTC)),
    ],
)
def test_known_transitions(zone: str, day: date, expected_start_utc: datetime) -> None:
    assert local_day_bounds(day, ZoneInfo(zone))[0] == expected_start_utc


def test_skipped_day_is_empty_interval() -> None:
    apia = ZoneInfo("Pacific/Apia")
    start, end = local_day_bounds(date(2011, 12, 30), apia)
    assert start == end
    assert local_day_bounds(date(2011, 12, 29), apia)[1] == start


def test_utc_remains_the_default_day() -> None:
    assert day_bounds(date(2026, 3, 8)) == (
        datetime(2026, 3, 8, tzinfo=UTC),
        datetime(2026, 3, 9, tzinfo=UTC),
    )


def test_resolve_rejects_aware_input() -> None:
    with pytest.raises(TypeError):
        resolve_wall_time(datetime(2026, 1, 1, tzinfo=UTC), ZoneInfo("UTC"))


@pytest.mark.parametrize("zone_name", MIDNIGHT_ZONES)
def test_exhaustive_every_day_1975_2037(zone_name: str) -> None:
    """Every calendar day in range, so every real transition in the tz database is exercised."""
    zone = ZoneInfo(zone_name)
    day, last = date(1975, 1, 1), date(2037, 12, 31)
    prev_end = local_day_bounds(day, zone)[0]
    odd_days = 0
    while day <= last:
        start, end = local_day_bounds(day, zone)
        midnight = datetime.combine(day, time.min)
        assert start == prev_end, day
        assert _wall(start, zone) >= midnight, day
        assert _wall(start - timedelta(microseconds=1), zone) < midnight, day
        assert timedelta(0) <= end - start <= timedelta(hours=26), day
        odd_days += (end - start) != timedelta(hours=24)
        prev_end = end
        day += timedelta(days=1)
    assert odd_days > 0  # sanity: every listed zone really had transitions in range
