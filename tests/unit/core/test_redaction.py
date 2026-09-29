"""Secrets never reach log output: top-level, nested, exception messages, chained tracebacks,
third-party (stdlib) loggers, reprs, warnings and uncaught exceptions."""

import io
import json
import logging
import sys
import warnings
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from edisc_core.logs import configure_logging, get_logger
from edisc_core.redaction import REDACTED, clear_registered_secrets, redact_value, register_secret

SECRET = "tok-CANARY-7f3a9c1e5b2d"  # registered, not pattern-shaped
SLACK_SHAPED = "xoxb-123456789012-abcdefABCDEF1234"  # never registered, caught by pattern


class Holder:
    def __init__(self, token: str) -> None:
        self.cfg = {"nested": [token]}

    def __repr__(self) -> str:
        return f"Holder({self.cfg!r})"


@pytest.fixture
def sink() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    old_hook = sys.excepthook
    configure_logging("DEBUG", stream=stream)
    register_secret(SECRET)
    yield stream
    clear_registered_secrets()
    sys.excepthook = old_hook
    logging.getLogger().handlers.clear()


def _assert_clean(output: str) -> None:
    assert output, "nothing was logged"
    assert SECRET not in output
    assert SLACK_SHAPED not in output
    assert "CANARY" not in output
    assert REDACTED in output


def test_top_level_and_sensitive_key_fields(sink: io.StringIO) -> None:
    get_logger("t").info("connect", token=SECRET, api_key="unregistered-value-123", note="keep-me")
    out = sink.getvalue()
    _assert_clean(out)
    assert "unregistered-value-123" not in out  # sensitive key => redacted even if unregistered
    assert "keep-me" in out  # no over-redaction


def test_nested_dicts_lists_and_innocent_keys(sink: io.StringIO) -> None:
    get_logger("t").info(
        "payload",
        request={
            "headers": {"Authorization": f"Bearer {SECRET}"},
            "params": [{"cursor": "abc"}, {"q": f"prefix {SECRET} suffix"}],
            "deep": {"a": {"b": {"c": [SLACK_SHAPED]}}},
        },
    )
    out = sink.getvalue()
    _assert_clean(out)
    assert '"cursor": "abc"' in out


def test_exception_message_and_traceback(sink: io.StringIO) -> None:
    def fetch() -> None:
        raise ValueError(f"upstream rejected token {SECRET} and {SLACK_SHAPED}")

    try:
        fetch()
    except ValueError:
        get_logger("t").exception("fetch failed")
    out = sink.getvalue()
    _assert_clean(out)
    assert "Traceback" in out
    assert "fetch failed" in out


def test_chained_exceptions(sink: io.StringIO) -> None:
    try:
        try:
            raise ConnectionError(f"auth header was Bearer {SECRET}")
        except ConnectionError as inner:
            raise RuntimeError(f"wrapped: {inner!r}") from inner
    except RuntimeError:
        get_logger("t").error("chain", exc_info=True)
    out = sink.getvalue()
    _assert_clean(out)
    assert "The above exception was the direct cause" in out


def test_third_party_stdlib_logger_args_and_extra(sink: io.StringIO) -> None:
    lib = logging.getLogger("botocore.fake")
    lib.warning("sending %s", {"token": SECRET}, extra={"payload": {"password": "p4ssw0rd-xyz"}})
    try:
        raise KeyError(SECRET)
    except KeyError:
        lib.exception("lib error %s", Holder(SECRET))
    out = sink.getvalue()
    _assert_clean(out)
    assert "p4ssw0rd-xyz" not in out


def test_repr_of_objects_and_secretstr(sink: io.StringIO) -> None:
    get_logger("t").info("objects", holder=Holder(SECRET), secret=SecretStr(SECRET), plain=SECRET)
    _assert_clean(sink.getvalue())


def test_warnings_routed_through_logging(sink: io.StringIO) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        # pytest swaps showwarning per test; captureWarnings is a no-op if already on, so reset it.
        logging.captureWarnings(False)
        logging.captureWarnings(True)
        warnings.warn(f"deprecated token {SECRET}", UserWarning, stacklevel=1)
    out = sink.getvalue()
    _assert_clean(out)
    assert "deprecated token" in out


def test_uncaught_exceptions(sink: io.StringIO) -> None:
    try:
        raise OSError(f"crash with {SECRET}")
    except OSError:
        sys.excepthook(*sys.exc_info())  # type: ignore[arg-type]
    out = sink.getvalue()
    _assert_clean(out)
    assert "uncaught exception" in out


def test_every_line_is_valid_json(sink: io.StringIO) -> None:
    get_logger("t").info("x", token=SECRET, nested={"k": [SECRET]})
    for line in sink.getvalue().splitlines():
        json.loads(line)


def test_redact_value_standalone() -> None:
    register_secret(SECRET)
    try:
        got = redact_value({"outer": ({"x": SECRET},), "client_secret": "abc", "n": 3, "ok": None})
    finally:
        clear_registered_secrets()
    assert got == {"outer": ({"x": REDACTED},), "client_secret": REDACTED, "n": 3, "ok": None}


def test_short_secrets_refused() -> None:
    with pytest.raises(ValueError, match="shorter than 8"):
        register_secret("abc")
