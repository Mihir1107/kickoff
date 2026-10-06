"""Streaming JSON array elements (metadata files of a Slack export, ADR 0014)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edisc_core.jsonstream import JsonStreamError, iter_array_elements
from tests.unit.turns import turns_during

JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=20,
)


async def _chunks(data: bytes, cuts: list[int]) -> AsyncIterator[bytes]:
    prev = 0
    for cut in sorted(c % (len(data) + 1) for c in cuts):
        yield data[prev:cut]
        prev = cut
    yield data[prev:]


def elements(data: bytes, cuts: list[int] = (), limit: int = 1 << 20) -> list[bytes]:  # type: ignore[assignment]
    async def run() -> list[bytes]:
        return [
            e async for e in iter_array_elements(_chunks(data, list(cuts)), max_element_bytes=limit)
        ]

    return asyncio.run(run())


@settings(max_examples=300, deadline=None)
@given(
    st.lists(JSON, max_size=8),
    st.sampled_from([None, 0, 2]),
    st.booleans(),
    st.lists(st.integers(0, 10_000), max_size=12),
)
def test_round_trip_with_any_chunking(
    values: list[object], indent: int | None, ascii_only: bool, cuts: list[int]
) -> None:
    data = json.dumps(values, indent=indent, ensure_ascii=ascii_only).encode()
    assert [json.loads(e) for e in elements(data, cuts)] == values


def test_strings_with_brackets_commas_and_escapes() -> None:
    values = ["a,b", "]", "[{", 'q"uote', "back\\slash", {"k]": [1, ",", {"x": "}"}]}]
    data = json.dumps(values).encode()
    for cut in range(len(data) + 1):  # every split point, including inside escapes
        assert [json.loads(e) for e in elements(data, [cut])] == values


@pytest.mark.parametrize(
    "data",
    [b"", b"   ", b'{"a": 1}', b"[1, 2", b"[1,,2]", b"[1, 2,]", b"[1] x", b'["abc'],
)
def test_malformed_input_is_rejected(data: bytes) -> None:
    with pytest.raises(JsonStreamError):
        elements(data)


def test_element_size_is_capped() -> None:
    big = json.dumps([{"x": "y" * 5000}]).encode()
    with pytest.raises(JsonStreamError, match="larger than"):
        elements(big, limit=1000)
    assert len(elements(big, limit=10_000)) == 1


def test_bom_and_empty_array() -> None:
    assert elements(b"\xef\xbb\xbf [ ] ") == []
    assert elements(b"\xef\xbb\xbf[1]", [1, 2]) == [b"1"]


async def test_a_source_that_never_suspends_still_gives_the_loop_a_turn_per_chunk() -> None:
    """Scanning is CPU work and a coalescing source serves megabytes without suspending: the
    scanner yields to the loop per chunk, so a large file never blocks heartbeats (ADR 0015 §24)."""
    data = json.dumps([{"ts": f"1.{i:06d}", "text": "x" * 200} for i in range(40_000)]).encode()
    chunks = -(-len(data) // (1 << 16))

    async def ready() -> AsyncIterator[bytes]:  # in-memory: never suspends
        for i in range(0, len(data), 1 << 16):
            yield data[i : i + (1 << 16)]

    async def scan_all() -> int:
        return len([e async for e in iter_array_elements(ready(), max_element_bytes=1 << 20)])

    turns, n = await turns_during(scan_all())
    assert n == 40_000
    assert turns >= chunks
