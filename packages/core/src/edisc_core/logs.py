"""Logging setup: structlog + stdlib, JSON lines, secret redaction on every path to a sink.

Call :func:`configure_logging` once at process start (API, worker, scripts). All stdlib loggers
(including third-party ones like botocore and temporalio) are routed through the same formatter, so
redaction cannot be bypassed by logging through a different library.
"""

from __future__ import annotations

import logging
import sys
from types import TracebackType
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger

from edisc_core.redaction import redact_text, redact_value

_EXEMPT_KEYS = frozenset({"exc_info", "_record", "_from_structlog"})


def _redact_event(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Structured-field redaction (sensitive keys at any depth, registered values, patterns)."""
    for key, value in list(event_dict.items()):
        if key in _EXEMPT_KEYS:
            continue
        redacted: Any = redact_value({key: value})[key]
        event_dict[key] = redacted
    return event_dict


class RedactingFormatter(structlog.stdlib.ProcessorFormatter):
    """Renders the record, then scrubs the final string (catches secrets inside tracebacks/reprs)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def _shared_processors() -> list[structlog.typing.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def build_handler(stream: TextIO | None = None, *, json: bool = True) -> logging.Handler:
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    )
    formatter = RedactingFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            _redact_event,
            renderer,
        ],
    )
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter)
    return handler


def configure_logging(
    level: int | str = "INFO", *, stream: TextIO | None = None, json: bool = True
) -> None:
    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(build_handler(stream, json=json))
    root.setLevel(level)
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught


def _log_uncaught(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger("edisc.uncaught").critical("uncaught exception", exc_info=(exc_type, exc, tb))


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger.bind(**initial) if initial else logger


def bind_context(**values: Any) -> None:
    """Bind request/job-scoped fields (tenant_id, job_id, unit_key) to all logs in this context."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


__all__ = [
    "RedactingFormatter",
    "bind_context",
    "build_handler",
    "clear_context",
    "configure_logging",
    "get_logger",
]
