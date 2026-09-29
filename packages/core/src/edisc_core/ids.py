"""Identifiers. UUIDv7 (RFC 9562): time-ordered, index-friendly. ``uuid.uuid7`` only lands in 3.14."""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0
_COUNTER_BITS = 12  # rand_a field, used as a monotonic counter within one millisecond


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7, monotonic within this process even for many IDs in the same millisecond."""
    global _last_ms, _counter
    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms = now_ms
            _counter = int.from_bytes(os.urandom(2), "big") & 0x3FF  # leave headroom
        else:
            _counter += 1
            if _counter >= 1 << _COUNTER_BITS:  # counter exhausted: borrow the next millisecond
                _last_ms += 1
                _counter = 0
        ms, counter = _last_ms, _counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= counter << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def new_id() -> uuid.UUID:
    return uuid7()


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: {value}")
    return value.int >> 80
