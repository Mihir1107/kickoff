"""Capture what the production logging setup writes (structlog + stdlib, redaction included).

Tests do not configure logging by default (structlog then prints to stdout, which ``caplog`` does not
see): a test that asserts a value is in NO log line installs the real configuration with
:func:`captured_logs` and reads the exact bytes a production sink would get.

A negative assertion ("the canary is in no line") is only worth something if the lines under test
really reached this sink. So the capture fails at exit when NOTHING was written, and tests prove
the specific lines arrived with :meth:`LogCapture.require` (a positive control) before asserting
what they must not contain. ``caplog`` is never used for that kind of assertion.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import structlog

from edisc_core.logs import configure_logging


class _Without(logging.Filter):
    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__()
        self.names = names

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(record.name == n or record.name.startswith(f"{n}.") for n in self.names)


class LogCapture(io.StringIO):
    """The sink's bytes, plus helpers to prove the expected lines are in it."""

    def events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in self.getvalue().splitlines():
            parsed = json.loads(line)  # every line of the production setup is one JSON object
            assert isinstance(parsed, dict), line
            out.append(parsed)
        return out

    def require(self, event: str, count: int = 1, **fields: Any) -> list[dict[str, Any]]:
        """At least ``count`` lines with this event and these field values (the positive control)."""
        hits = [
            e for e in self.events()
            if e.get("event") == event and all(e.get(k) == v for k, v in fields.items())
        ]  # fmt: skip
        assert len(hits) >= count, (
            f"expected >= {count} {event!r} lines with {fields}, got {len(hits)}: the negative "
            f"assertions of this test would be blind\n{self.getvalue()[-3000:]}"
        )
        return hits


@contextlib.contextmanager
def captured_logs(*, without: tuple[str, ...] = ("httpx",)) -> Iterator[LogCapture]:
    """``without``: loggers left out. By default ``httpx``: the TEST's own client logs its request
    line (the in-process API and the test share one process)."""
    old_config = structlog.get_config()
    root = logging.getLogger()
    old_handlers, old_level, old_hook = list(root.handlers), root.level, sys.excepthook
    stream = LogCapture()
    configure_logging("DEBUG", stream=stream)
    for handler in root.handlers:
        handler.addFilter(_Without(without))
    try:
        yield stream
    finally:
        structlog.configure(**old_config)
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in old_handlers:
            root.addHandler(handler)
        root.setLevel(old_level)
        sys.excepthook = old_hook
    assert stream.getvalue(), "nothing reached the production logging pipeline (a blind capture)"
