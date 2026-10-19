"""When a collection refreshes its items' statistics: once per doubling of the items the job
inserted, from 1,024 on (``edisc_worker.pipeline.statistics_due``, ADR 0015 §24.20)."""

from __future__ import annotations

from edisc_worker.pipeline import STATISTICS_FIRST, statistics_due


def test_once_per_power_of_two_from_the_first() -> None:
    assert STATISTICS_FIRST == 1024
    assert not statistics_due(0, 1023)
    assert statistics_due(1000, 1024)
    assert statistics_due(1023, 1500)
    assert not statistics_due(1024, 2047)
    assert statistics_due(2047, 2048)
    assert statistics_due(0, 5000)  # one batch over several: one refresh
    assert not statistics_due(5000, 5000)


def test_batches_of_any_size_refresh_log2_times() -> None:
    # 1,024 ... 262,144: nine powers of two below 330,000; a first batch of 3,000 crosses 1,024 and
    # 2,048 at once and refreshes once
    for size, expected in ((1, 9), (7, 9), (200, 9), (500, 9), (3000, 8)):
        total, refreshes = 0, 0
        while total < 330_000:
            refreshes += statistics_due(total, total + size)
            total += size
        assert refreshes == expected, (size, refreshes)
