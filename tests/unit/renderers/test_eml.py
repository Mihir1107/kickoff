"""Header encoding and folding (ADR 0015 §5): deterministic, RFC 5322/2047, and lossless."""

from __future__ import annotations

import asyncio
import base64
import email
import email.policy
from collections.abc import AsyncIterator

import pytest
from hypothesis import given
from hypothesis import strategies as st

from edisc_core import loopguard
from edisc_renderers.rsmf.eml import aenvelope, base64_lines, header, rfc5322_date

NAMES = ("Subject", "X-RSMF-Participants", "X-RSMF-Custodian")


def _round_trip(name: str, value: str) -> str:
    raw = header(name, value) + b"\r\n"
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return str(msg[name])


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        ", ".join(f"User{i:02d} Example" for i in range(30)),
        "Zoë, ✓, 李, \U0001f469‍\U0001f469‍\U0001f467, שלום, Z͑ͫ̓̀",
        "a" * 300,
        "<" + "f" * 64 + "@rsmf.edisc>",
    ],
)
def test_headers_round_trip_and_fold(value: str) -> None:
    for name in NAMES:
        raw = header(name, value)
        assert raw.isascii() and raw.endswith(b"\r\n")
        lines = raw[:-2].split(b"\r\n")
        assert all(len(line) <= 998 for line in lines)
        if not value.isascii() or " " in value:
            assert all(len(line) <= 78 for line in lines), lines
        assert all(line.startswith(b" ") for line in lines[1:])
        assert _round_trip(name, value) == value


def test_long_ascii_tokens_are_not_encoded() -> None:
    # a Message-ID or a hash is a structured token: never RFC 2047, even past 78 characters
    raw = header("X-RSMF-SourceHash", "f" * 64)
    assert raw == b"X-RSMF-SourceHash: " + b"f" * 64 + b"\r\n"


@given(
    st.text(min_size=1, max_size=300).filter(
        lambda s: s == s.strip() and "\r" not in s and "\n" not in s
    )
)
def test_any_text_round_trips(value: str) -> None:
    assert _round_trip("X-RSMF-Participants", value) == value


@given(st.lists(st.binary(max_size=300), max_size=20))
def test_base64_lines_match_the_standard_encoding(chunks: list[bytes]) -> None:
    data = b"".join(chunks)
    out = b"".join(base64_lines(chunks))
    assert out == base64.encodebytes(data).replace(b"\n", b"\r\n")


def test_rfc5322_date() -> None:
    from edisc_core.time import parse_utc

    assert (
        rfc5322_date(parse_utc("2026-01-05T23:59:59.999000Z")) == "Mon, 05 Jan 2026 23:59:59 +0000"
    )
    assert rfc5322_date(parse_utc("2026-02-28T00:00:00+05:30")) == "Fri, 27 Feb 2026 18:30:00 +0000"


async def test_the_envelope_gives_the_loop_a_turn_per_chunk() -> None:
    """A zip stream whose chunks are ready without suspending (buffered reads) must not keep the
    event loop for the whole multi-GB attachment (ADR 0015 §24)."""
    chunk = bytes(range(256)) * 256  # 64 KiB

    async def ready() -> AsyncIterator[bytes]:
        for _ in range(3_000):  # about 190 MiB, never suspending
            yield chunk

    blocks: list[loopguard.Block] = []
    guard = loopguard.LoopGuard(asyncio.get_running_loop(), 100, blocks.append)
    guard.start()
    try:
        total = 0
        async for out in aenvelope([("Subject", "s")], "b", "summary", ready()):
            total += len(out)
    finally:
        guard.stop()
    assert total > 3_000 * len(chunk)
    assert [b for b in blocks if b.counts] == []
