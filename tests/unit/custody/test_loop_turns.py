"""Readers give the event loop a turn per chunk (ADR 0015 §24), and the synchronous drivers of the
offline verifier step over such a turn while still refusing a real suspension."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Generator
from typing import Any

import pytest

from edisc_custody import package_source, rsmf_check
from edisc_custody.archive import ArchiveLimits, BytesSource, read_entry, scan
from tests.unit.turns import turns_during


async def _turns() -> int:
    for _ in range(3):
        await asyncio.sleep(0)
    return 7


class _Suspends:
    def __await__(self) -> Generator[Any, None, int]:
        yield "a future"  # what a real I/O wait yields
        return 1


@pytest.mark.parametrize("run", [package_source._run, rsmf_check._run])
def test_the_synchronous_drivers_step_over_turns_but_refuse_suspensions(run: Any) -> None:
    assert run(_turns()) == 7
    with pytest.raises(RuntimeError, match="suspended"):
        run(_Suspends())


async def test_a_large_entry_from_a_ready_source_gives_the_loop_a_turn_per_chunk() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.bin", bytes(range(256)) * (1 << 14))  # 4 MiB, compresses well
    limits = ArchiveLimits(max_entry_ratio=10_000, max_total_ratio=10_000, read_chunk=1 << 12)
    src = BytesSource(buf.getvalue())  # memory: never suspends
    (entry,) = await scan(src, limits)
    chunks = -(-entry.compressed_size // limits.read_chunk)
    turns, (data, digest) = await turns_during(read_entry(src, entry, limits))
    assert digest.size == len(data) == 1 << 22
    assert turns >= chunks
