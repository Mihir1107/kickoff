"""Capture what the production logging setup writes (structlog + stdlib, redaction included).

Tests do not configure logging by default (structlog then prints to stdout, which ``caplog`` does not
see): a test that asserts a value is in NO log line installs the real configuration with
:func:`captured_logs` and reads the exact bytes a production sink would get.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
from collections.abc import Iterator

import structlog

from edisc_core.logs import configure_logging


class _Without(logging.Filter):
    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__()
        self.names = names

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(record.name == n or record.name.startswith(f"{n}.") for n in self.names)


@contextlib.contextmanager
def captured_logs(*, without: tuple[str, ...] = ("httpx",)) -> Iterator[io.StringIO]:
    """``without``: loggers left out. By default ``httpx``: the TEST's own client logs its request
    line (the in-process API and the test share one process)."""
    old_config = structlog.get_config()
    root = logging.getLogger()
    old_handlers, old_level, old_hook = list(root.handlers), root.level, sys.excepthook
    stream = io.StringIO()
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
