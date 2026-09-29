from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from edisc_core.time import (
    NaiveDatetimeError,
    NonexistentLocalTimeError,
    UtcDatetime,
    day_bounds,
    ensure_utc,
    format_utc,
    from_epoch,
    parse_utc,
    utc_day,
    utc_now,
)

NAIVE = datetime(2026, 1, 2, 3, 4, 5)  # noqa: DTZ001 - deliberately naive


@pytest.mark.parametrize("fn", [ensure_utc, format_utc, utc_day])
def test_every_datetime_entry_point_rejects_naive(fn: object) -> None:
    with pytest.raises(NaiveDatetimeError):
        fn(NAIVE)  # type: ignore[operator]


@pytest.mark.parametrize("text", ["2026-01-02T03:04:05", "2026-01-02 03:04:05.123", "2026-01-02"])
def test_parse_rejects_offsetless_strings(text: str) -> None:
    with pytest.raises(NaiveDatetimeError):
        parse_utc(text)


def test_pydantic_field_rejects_naive_and_normalizes_offsets() -> None:
    class M(BaseModel):
        at: UtcDatetime

    with pytest.raises(ValidationError, match="naive datetime rejected"):
        M(at=NAIVE)
    with pytest.raises(ValidationError):
        M.model_validate({"at": "2026-01-02T03:04:05"})
    m = M.model_validate({"at": "2026-01-02T05:04:05+02:00"})
    assert m.at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert m.at.tzinfo is UTC


def test_ensure_utc_rejects_non_datetime() -> None:
    with pytest.raises(TypeError):
        ensure_utc(date(2026, 1, 1))  # type: ignore[arg-type]


@given(st.datetimes(timezones=st.timezones()))
def test_ensure_utc_preserves_the_instant_or_rejects_dst_gap_times(value: datetime) -> None:
    try:
        converted = ensure_utc(value)
    except NonexistentLocalTimeError:
        roundtrip = value.astimezone(UTC).astimezone(value.tzinfo)
        assert roundtrip.replace(tzinfo=None, fold=0) != value.replace(tzinfo=None, fold=0)
        return
    # Compare instants via timestamp(): Python's inter-zone == is False for fold/gap datetimes.
    assert converted.timestamp() == value.timestamp()
    assert converted.utcoffset() == timedelta(0)


def test_dst_gap_time_rejected_and_fold_resolved() -> None:
    ny = ZoneInfo("America/New_York")
    with pytest.raises(NonexistentLocalTimeError):
        ensure_utc(datetime(2026, 3, 8, 2, 30, tzinfo=ny))  # clocks jump 02:00 -> 03:00
    first = ensure_utc(datetime(2026, 11, 1, 1, 30, tzinfo=ny, fold=0))
    second = ensure_utc(datetime(2026, 11, 1, 1, 30, tzinfo=ny, fold=1))
    assert second - first == timedelta(hours=1)


def test_format_is_fixed_width_z() -> None:
    tz = timezone(timedelta(hours=-5))
    assert format_utc(datetime(2026, 3, 1, 19, 0, tzinfo=tz)) == "2026-03-02T00:00:00.000000Z"


def test_utc_day_uses_utc_not_local_day() -> None:
    tz = timezone(timedelta(hours=10))
    assert utc_day(datetime(2026, 3, 2, 5, 0, tzinfo=tz)) == date(2026, 3, 1)


def test_day_bounds_half_open() -> None:
    start, end = day_bounds(date(2026, 2, 28))
    assert start == datetime(2026, 2, 28, tzinfo=UTC)
    assert end - start == timedelta(days=1)


def test_from_epoch_keeps_slack_ts_microseconds_exactly() -> None:
    assert from_epoch("1712345678.000123") == datetime(2024, 4, 5, 19, 34, 38, 123, tzinfo=UTC)
    assert from_epoch("1712345678") == datetime(2024, 4, 5, 19, 34, 38, tzinfo=UTC)


def test_utc_now_is_aware() -> None:
    assert utc_now().tzinfo is UTC
