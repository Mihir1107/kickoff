"""No error path echoes a request value (M17 step 0; ADR 0016 amendment, decision D11).

A credential sent in the wrong field, with the wrong type or too long must never come back: not in
a response, not in a log line. Before this module FastAPI's default 422 body carried each error's
``input`` (the value itself) and ``ctx``, so ``{"token": "xoxb-..."}`` sent to a model without that
field was answered with the token.

- **Validation errors** (:func:`validation_error_body`): ``type``, a sanitised ``loc`` and a message
  that depends on the error type and the schema only. Never ``input``, ``ctx`` or ``url``. A ``loc``
  element the client chose (an extra key, a dict key) is replaced by ``*``: only names our request
  models and parameters declare, list indexes and the location kind are kept. Pydantic's message is
  kept only for types whose message is built from the schema (constraints, expected literals);
  anything else gets a fixed text. Our own validators raise ``PydanticCustomError`` with a type
  prefixed ``edisc_`` and a static message.
- **Unhandled exceptions** (:class:`SafeErrors`, the outermost middleware): a 500 with a fixed body
  and the request id; the log line names the exception types and frames (file, line, function),
  never the message (a database error's text holds the statement's parameters, a pydantic error's
  the input). After the first byte of a response nothing can be answered: the error is logged the
  same way and re-raised so the transfer breaks (those are integrity failures of stored objects,
  which never hold request values).
- **Log lines** carry the path with every segment that is not a static segment of a route
  replaced (:func:`safe_path`); never the query string. Uvicorn's own access log (the raw request
  line) is switched off by :func:`route_server_logs`; the API writes its own access line.
- The one exception is ``X-Request-Id``: echoing it is its purpose (a client's correlation id,
  restricted to 128 characters of ``[A-Za-z0-9_-]``).
"""

from __future__ import annotations

import inspect
import logging
import re
import traceback
import typing
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.params import Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from edisc_core.logs import get_logger

log = get_logger(__name__)

LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})
CLIENT_CHOSEN = "*"

# Pydantic messages built from the schema alone (constraints, expected values), never the input.
SCHEMA_MESSAGES = frozenset({
    "missing", "extra_forbidden", "string_type", "string_too_short", "string_too_long",
    "string_pattern_mismatch", "string_unicode", "int_type", "int_parsing", "int_from_float",
    "int_parsing_size", "float_type", "float_parsing", "finite_number", "bool_type",
    "bool_parsing", "literal_error", "enum", "greater_than", "greater_than_equal", "less_than",
    "less_than_equal", "multiple_of", "too_short", "too_long", "dict_type", "list_type",
    "tuple_type", "set_type", "model_type", "model_attributes_type", "dataclass_type",
    "uuid_type", "date_type", "datetime_type", "time_type", "timezone_aware", "timezone_naive",
    "none_required", "bytes_type", "bytes_too_short", "bytes_too_long", "is_instance_of",
    "union_tag_invalid", "union_tag_not_found", "frozen_field", "frozen_instance",
    "no_such_attribute", "missing_argument", "unexpected_keyword_argument",
})  # fmt: skip
# Types whose pydantic message may quote part of the input: a fixed text instead.
FIXED_MESSAGES: Mapping[str, str] = {
    "json_invalid": "Invalid JSON",
    "json_type": "Input should be a valid JSON document",
    "uuid_parsing": "Input should be a valid UUID",
    "uuid_version": "Input should be a UUID of the expected version",
    "date_parsing": "Input should be a valid date in the format YYYY-MM-DD",
    "date_from_datetime_parsing": "Input should be a valid date",
    "date_from_datetime_inexact": "Input should be a date without a time part",
    "datetime_parsing": "Input should be a valid datetime",
    "datetime_from_date_parsing": "Input should be a valid datetime",
    "time_parsing": "Input should be a valid time",
    "decimal_parsing": "Input should be a valid decimal",
    "url_parsing": "Input should be a valid URL",
    "url_scheme": "URL scheme not permitted",
    "value_error": "Invalid value",
    "assertion_error": "Invalid value",
}
OTHER_MESSAGE = "Invalid value"
OWN_PREFIX = "edisc_"  # PydanticCustomError types raised by our validators, static messages


def _message(error: Mapping[str, Any]) -> str:
    kind = str(error.get("type", ""))
    if kind in SCHEMA_MESSAGES or kind.startswith(OWN_PREFIX):
        return str(error.get("msg", OTHER_MESSAGE))
    return FIXED_MESSAGES.get(kind, OTHER_MESSAGE)


def _loc(loc: Sequence[Any], names: frozenset[str]) -> list[str | int]:
    out: list[str | int] = []
    for i, part in enumerate(loc):
        if isinstance(part, int) and not isinstance(part, bool):
            out.append(part)  # a list index or a JSON error position: chosen by us, not a value
        elif isinstance(part, str) and (part in names or (i == 0 and part in LOCATIONS)):
            out.append(part)
        else:
            out.append(CLIENT_CHOSEN)
    return out


def validation_error_body(
    errors: Iterable[Mapping[str, Any]], names: frozenset[str]
) -> dict[str, Any]:
    return {
        "error": "validation_error",
        "detail": [
            {
                "type": str(e.get("type", "")),
                "loc": _loc(e.get("loc", ()), names),
                "msg": _message(e),
            }
            for e in errors
        ],
    }


# ------------------------------------------------------------------------- declared names
def _flatten(tp: Any) -> Iterable[Any]:
    yield tp
    for arg in typing.get_args(tp):
        if not isinstance(arg, (str, int, float, bool, FieldInfo, Depends)) and arg is not None:
            yield from _flatten(arg)


def _model_names(tp: Any, names: set[str], seen: set[type]) -> None:
    for t in _flatten(tp):
        if isinstance(t, type) and issubclass(t, BaseModel) and t not in seen:
            seen.add(t)
            for name, field in t.model_fields.items():
                names.add(name)
                for alias in (field.alias, field.validation_alias):
                    if isinstance(alias, str):
                        names.add(alias)
                _model_names(field.annotation, names, seen)


def _callable_names(fn: Callable[..., Any], names: set[str], seen: set[Any]) -> None:
    if fn in seen:
        return
    seen.add(fn)
    target = fn if inspect.isfunction(fn) or inspect.ismethod(fn) else type(fn).__call__
    hints = typing.get_type_hints(target, include_extras=True)
    for pname, param in inspect.signature(fn).parameters.items():
        hint = hints.get(pname, param.annotation)
        metadata = typing.get_args(hint)[1:] if typing.get_origin(hint) is typing.Annotated else ()
        for meta in (*metadata, param.default):
            if isinstance(meta, Depends) and meta.dependency is not None:
                _callable_names(meta.dependency, names, seen)
                break
        else:
            names.add(pname)
            names.add(pname.replace("_", "-"))  # header parameters are matched with hyphens
            for meta in metadata:
                if isinstance(meta, FieldInfo) and isinstance(meta.alias, str):
                    names.update({meta.alias, meta.alias.lower()})
            _model_names(hint, names, seen)


def declared_names(endpoints: Iterable[Callable[..., Any]]) -> frozenset[str]:
    """Every parameter and request-model field name (and alias) the given endpoints declare,
    through their dependencies: the only strings a sanitised ``loc`` keeps."""
    names: set[str] = set()
    seen: set[Any] = set()
    for fn in endpoints:
        _callable_names(fn, names, seen)
    return frozenset(names)


def static_segments(paths: Iterable[str]) -> frozenset[str]:
    return frozenset(seg for path in paths for seg in path.split("/") if seg and "{" not in seg)


def safe_path(path: str, segments: frozenset[str]) -> str:
    """The path with every segment that is not a static segment of a route replaced by ``*``."""
    return "/".join(seg if not seg or seg in segments else CLIENT_CHOSEN for seg in path.split("/"))


# ------------------------------------------------------------------------- wiring
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _frames(exc: BaseException, limit: int = 12) -> list[str]:
    out = []
    for frame in traceback.extract_tb(exc.__traceback__)[-limit:]:
        parts = frame.filename.replace("\\", "/").split("/")
        out.append(f"{'/'.join(parts[-3:])}:{frame.lineno} {frame.name}")
    return out


def _types(exc: BaseException) -> list[str]:
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(f"{type(current).__module__}.{type(current).__qualname__}")
        current = current.__cause__ or current.__context__
    return chain


def log_unhandled(exc: BaseException, *, path: str, request_id: str | None, started: bool) -> None:
    """Types and frames only: an exception's text may hold request values (never logged)."""
    log.error(
        "unhandled error", error_types=_types(exc), frames=_frames(exc), path=path,
        request_id=request_id, response_started=started,
    )  # fmt: skip


class ResponseCut(Exception):  # noqa: N818 (an intended reset, not an error)
    """Raised by a response body after its first byte to make the server reset the connection
    (never complete it): a download cut by revocation (``edisc_api.downloads.DownloadCut``)."""


class SafeErrors:
    """Outermost middleware: an unhandled exception becomes a fixed 500 (before the first byte) and
    a log line without its message; after the first byte it is logged the same way and re-raised."""

    def __init__(self, app: ASGIApp, segments: Callable[[], frozenset[str]]) -> None:
        self.app, self.segments = app, segments

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except ResponseCut:
            raise  # deliberate (edisc_api.downloads logged and audited it): reset, never a 500
        except Exception as exc:
            rid = _request_id(scope)
            log_unhandled(
                exc, path=safe_path(scope.get("path", ""), self.segments()), request_id=rid,
                started=started,
            )  # fmt: skip
            if started:
                raise
            response = JSONResponse(
                {"error": "internal_error", "detail": "internal error", "request_id": rid},
                status_code=500,
                headers={"x-request-id": rid} if rid else None,
            )
            await response(scope, receive, send)


def _request_id(scope: Scope) -> str | None:
    state = scope.get("state")
    rid = state.get("request_id") if isinstance(state, Mapping) else None
    return rid if isinstance(rid, str) and _REQUEST_ID.match(rid) else None


# the framework's own refusals, as registered codes (edisc_api.error_codes)
HTTP_CODES = {
    400: ("malformed_body", "the request body could not be parsed"),
    404: ("not_found", "not found"),
    405: ("method_not_allowed", "method not allowed"),
}


def install(app: FastAPI, routers: Iterable[Any]) -> None:
    """Register the validation handler and the outermost 500 middleware for ``app``. Call after
    every other middleware, so this one wraps them all."""
    routes = [r for router in routers for r in router.routes]
    names = declared_names(r.endpoint for r in routes)
    segments = static_segments(r.path for r in routes)
    app.state.safe_path_segments = segments

    async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
        errors = exc.errors() if isinstance(exc, RequestValidationError) else ()
        return JSONResponse(validation_error_body(errors, names), status_code=422)

    async def _http_error(_: Request, exc: Exception) -> Response:
        # Starlette's and FastAPI's own refusals (an unparsable body, no route, a method the path
        # does not have) answer with a registered code and a fixed text, like every other error
        status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        code, detail = HTTP_CODES.get(status, ("internal_error", "internal error"))
        headers = getattr(exc, "headers", None)
        return JSONResponse({"error": code, "detail": detail},
                            status_code=status if status in HTTP_CODES else 500, headers=headers)  # fmt: skip

    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_middleware(SafeErrors, segments=lambda: segments)


def request_path(request: Request) -> str:
    """The request's path for log lines (:func:`safe_path`)."""
    segments: frozenset[str] = getattr(request.app.state, "safe_path_segments", frozenset())
    return safe_path(request.url.path, segments)


def route_server_logs() -> None:
    """Uvicorn configures its own handlers before importing the app: send its error log through
    ours (redaction included) and silence its access log, which prints the raw request line
    (path and query string). The API's middleware writes the access line instead."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.asgi"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    for handler in list(access.handlers):
        access.removeHandler(handler)
    access.propagate = False
    access.disabled = True


__all__ = [
    "SafeErrors",
    "declared_names",
    "install",
    "request_path",
    "route_server_logs",
    "safe_path",
    "static_segments",
    "validation_error_body",
]
