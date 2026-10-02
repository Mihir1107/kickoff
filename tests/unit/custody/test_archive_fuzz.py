"""Property-based fuzzing of the ZIP reader (ADR 0014 R5).

Invariant: for EVERY input (valid archives mutated, truncated, spliced, with header fields forced to
extreme values) the reader either parses within its limits or raises a classified ``ArchiveError``:
never another exception, never a hang, never unbounded memory, never more bytes than a limit allows.

CI runs a short budget. Longer manual run (documented in docs/runs/README.md):
    EDISC_FUZZ_EXAMPLES=20000 uv run pytest tests/unit/custody/test_archive_fuzz.py -p no:randomly
"""

from __future__ import annotations

import asyncio
import os
import struct
import time
import tracemalloc
import zipfile

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from edisc_custody.archive import ArchiveError, ArchiveLimits, BytesSource, open_entry, scan

from .zips import make_zip, zip64_central_directory

EXAMPLES = int(os.environ.get("EDISC_FUZZ_EXAMPLES", "300"))
LIMITS = ArchiveLimits(
    max_entries=64,
    max_entry_bytes=256 * 1024,
    max_total_bytes=1024 * 1024,
    max_total_ratio=1000,
    max_entry_ratio=200,
    ratio_floor_bytes=8 * 1024,
    max_name_bytes=64,
    read_chunk=4096,
)
NAMES = st.sampled_from(
    ["users.json", "channels.json", "general/2026-01-05.json", "general/2026-01-06.json",
     "random/2026-01-05.json", "D1/2026-01-05.json", "dir/", "x.json"]
)  # fmt: skip
BODIES = st.one_of(
    st.binary(max_size=2048),
    st.integers(0, 300_000).map(lambda n: b"\0" * n),  # compressible (bomb-like)
    st.text(max_size=500).map(lambda t: t.encode()),
)


@st.composite
def archives(draw: st.DrawFn) -> bytes:
    entries = draw(st.dictionaries(NAMES, BODIES, min_size=1, max_size=6))
    entries = {k: (b"" if k.endswith("/") else v) for k, v in entries.items()}
    method = draw(st.sampled_from([zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]))
    data = make_zip(entries, method=method, zip64=draw(st.booleans()))
    if draw(st.booleans()):
        data = zip64_central_directory(data)
    for _ in range(draw(st.integers(0, 4))):
        kind = draw(
            st.sampled_from(
                ["flip", "truncate", "splice", "field", "append", "header", "header", "header"]
            )
        )
        if not data:
            break
        if kind == "flip":
            i = draw(st.integers(0, len(data) - 1))
            data = data[:i] + bytes([data[i] ^ draw(st.integers(1, 255))]) + data[i + 1 :]
        elif kind == "truncate":
            data = data[: draw(st.integers(0, len(data)))]
        elif kind == "splice":
            a = draw(st.integers(0, len(data)))
            b = draw(st.integers(a, min(len(data), a + 200)))
            at = draw(st.integers(0, len(data)))
            data = data[:at] + data[a:b] + data[at:]
        elif kind == "field" and len(data) >= 4:
            i = draw(st.integers(0, len(data) - 4))
            value = draw(st.sampled_from([0, 1, 0xFFFF, 0xFFFFFFFF, 0x7FFFFFFF, len(data), 46]))
            out = bytearray(data)
            struct.pack_into("<I", out, i, value)
            data = bytes(out)
        elif kind == "header":
            # structure-aware: overwrite a field of a real record (local, central, zip64, end records),
            # including the extra-field area that follows a central-directory header
            sigs = [i for sig in SIGNATURES for i in _find_all(data, sig)]
            if not sigs:
                continue
            at = draw(st.sampled_from(sigs)) + draw(st.integers(4, 80))
            width = draw(st.sampled_from([2, 4, 8]))
            if at + width > len(data):
                continue
            value = draw(st.sampled_from([0, 1, 2, 7, 8, 16, 0xFF, 0xFFFF, 0xFFFFFFFF, len(data)]))
            out = bytearray(data)
            struct.pack_into(
                {2: "<H", 4: "<I", 8: "<Q"}[width], out, at, value % (1 << (8 * width))
            )
            data = bytes(out)
        else:
            data = data + draw(st.binary(max_size=64))
    return data


SIGNATURES = (b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06", b"PK\x06\x06", b"PK\x06\x07")


def _find_all(data: bytes, sig: bytes) -> list[int]:
    out, i = [], data.find(sig)
    while i >= 0:
        out.append(i)
        i = data.find(sig, i + 1)
    return out


async def _exercise(data: bytes) -> int:
    """Validate everything, then read every entry again counting produced bytes."""
    src = BytesSource(data)
    entries = await scan(src, LIMITS)
    produced = 0
    for e in entries:
        size = 0
        async for chunk in open_entry(src, e, LIMITS):
            size += len(chunk)
            assert size <= LIMITS.max_entry_bytes  # no escape of the per-entry limit
        produced += size
    return produced


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(archives())
def test_every_input_parses_within_limits_or_is_rejected_with_a_classified_error(
    data: bytes,
) -> None:
    tracemalloc.start()
    started = time.monotonic()
    try:
        produced = asyncio.run(_exercise(data))
    except ArchiveError:
        produced = 0  # classified rejection: the only acceptable failure
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert produced <= LIMITS.total_cap(len(data))
    assert peak < 16 * 1024 * 1024, f"peak memory {peak} bytes"
    assert time.monotonic() - started < 10, "no input may take long (hang guard)"
