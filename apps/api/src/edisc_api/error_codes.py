"""Every error code the API answers, in ONE registry (before M17 step 5; plan §5 "error codes").

An error answer is ``{"error": <code>, ...}`` (JSON), or, for the browser sign-in, the bounce page to
``/?auth_error=<code>``. Each code has ONE status. A route declares the codes its own handler can
answer (``authz.perm(P.X, E.NOT_FOUND, ...)``, or :func:`declare` for the routes without a
permission); :func:`complete` adds the codes that follow from the operation's shape and writes the
list into the document as ``x-error-codes``, plus one response per status naming its codes:

- every operation: ``internal_error`` (500), ``db_pool_exhausted`` and ``service_busy`` (503);
- an operation with parameters or a body: ``validation_error`` (422); with a body also
  ``malformed_body`` (400: the framework could not parse it);
- an authenticated operation (``x-permission`` other than ``public``): ``unauthorized``,
  ``too_many_requests`` (the authentication throttle), ``ambiguous_credentials``, ``cross_site``,
  and ``csrf`` for a state change;
- a list (a ``cursor`` query parameter): ``cursor_mismatch`` and ``unprocessable`` (a bad cursor).

The API test client fails a test on any error answer whose code is not declared for the operation
that answered it, or whose status is not the code's (``tests/integration/api/conftest.py``), so the
declarations cannot drift from the code. ``scripts/api_compat.py`` fails a change that removes a
declared code (``edisc-error-code-removed``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Code:
    code: str
    status: int
    meaning: str


class E:
    """The registry. A new code is added here, with its one status."""

    # authentication and the request itself (mostly added by complete())
    UNAUTHORIZED = Code("unauthorized", 401, "no valid credentials, or an unknown tenant host")
    TOO_MANY_REQUESTS = Code("too_many_requests", 429, "too many failed attempts from this address")
    TOO_MANY_STREAMS = Code("too_many_streams", 429, "too many open streams for this caller")
    AMBIGUOUS_CREDENTIALS = Code(
        "ambiguous_credentials", 400, "a session cookie AND a bearer token"
    )
    CROSS_SITE = Code("cross_site", 403, "a cookie-authenticated request from another site")
    CSRF = Code("csrf", 403, "a state change without a valid X-CSRF-Token or same Origin")
    VALIDATION_ERROR = Code("validation_error", 422, "the request does not match the schema")
    MALFORMED_BODY = Code("malformed_body", 400, "the request body could not be parsed")
    METHOD_NOT_ALLOWED = Code("method_not_allowed", 405, "the path has no such method")
    CURSOR_MISMATCH = Code("cursor_mismatch", 422, "a cursor from another route or filters")
    UNPROCESSABLE = Code("unprocessable", 422, "a well-formed request the API cannot act on")
    INTERNAL_ERROR = Code("internal_error", 500, "an unexpected server error")
    DB_POOL_EXHAUSTED = Code("db_pool_exhausted", 503, "every database connection is busy; retry")
    SERVICE_BUSY = Code("service_busy", 503, "Redis (throttle, limits) busy or unreachable; retry")
    # authorization
    NOT_FOUND = Code("not_found", 404, "no such object, or not visible to the caller")
    FORBIDDEN = Code("forbidden", 403, "visible, but the caller lacks the permission")
    REAUTH_REQUIRED = Code("reauth_required", 401, "the action needs a recent sign-in")
    # state
    CONFLICT = Code("conflict", 409, "the object's state does not allow it")
    ALREADY_REVOKED = Code("already_revoked", 409, "the session is already revoked")
    CLIENT_CLOSED = Code("client_closed", 409, "the client is closed")
    MATTER_CLOSED = Code("matter_closed", 409, "the matter is closed")
    JOB_NOT_SEALED = Code("job_not_sealed", 409, "the job's custody chain is not sealed yet")
    RENDER_NOT_SEALED = Code("render_not_sealed", 409, "the render is not sealed yet")
    REPORT_NOT_SEALED = Code("report_not_sealed", 409, "the report is not sealed yet")
    INTEGRITY = Code("integrity", 409, "a stored object does not match its record")
    PREVIEW_INTEGRITY = Code("preview_integrity", 500, "the day's evidence did not verify")
    NOT_IN_SCOPE = Code("not_in_scope", 404, "the day is outside every scope of the job")
    PAGE_NOT_FOUND = Code("page_not_found", 404, "the day has no such page")
    UNKNOWN_TIME_ZONE = Code("unknown_time_zone", 422, "not an IANA name of the pinned tzdata")
    # exports
    UPLOAD_EXPIRED = Code("upload_expired", 410, "the upload expired")
    PART_TOO_LARGE = Code("part_too_large", 413, "the part exceeds the upload's part size")
    EXPORT_TOO_LARGE = Code("export_too_large", 413, "the declared export exceeds the limit")
    MISSING_DIGEST = Code("missing_digest", 400, "the part has no Content-Digest sha-256")
    DIGEST_MISMATCH = Code("digest_mismatch", 422, "the part's bytes differ from its digest")
    DECLARED_HASH_MISMATCH = Code(
        "declared_hash_mismatch", 422, "the upload differs from its declared SHA-256"
    )
    # browser sessions
    NOT_A_SESSION = Code("not_a_session", 400, "a route for browser sessions called without one")
    INVALID_RETURN_TO = Code("invalid_return_to", 400, "return_to is not a same-origin path")
    IDP_UNAVAILABLE = Code("idp_unavailable", 502, "the identity provider could not be used")
    # the sign-in bounce page (/?auth_error=<code>; ADR 0016 §1 and its notes)
    DENIED = Code("denied", 400, "the user refused at the identity provider")
    EXPIRED = Code("expired", 400, "the sign-in took too long")
    INVALID = Code("invalid", 400, "the sign-in failed a check")
    UNKNOWN_USER = Code("unknown_user", 401, "no principal for this identity")
    INACTIVE = Code("inactive", 401, "the principal is deactivated")
    REAUTH_MISMATCH = Code("reauth_mismatch", 403, "the re-authentication was not the same user")
    GROUPS_OVERAGE = Code("groups_overage", 403, "the IdP sent a groups overage")
    GROUPS_MISSING = Code("groups_missing", 403, "no usable groups claim while roles need one")


REGISTRY: dict[str, Code] = {c.code: c for c in vars(E).values() if isinstance(c, Code)}
UNSAFE = frozenset({"post", "put", "patch", "delete"})
EVERY = (E.INTERNAL_ERROR, E.DB_POOL_EXHAUSTED, E.SERVICE_BUSY)
AUTHENTICATED = (E.UNAUTHORIZED, E.TOO_MANY_REQUESTS, E.AMBIGUOUS_CREDENTIALS, E.CROSS_SITE)
LIST = (E.CURSOR_MISMATCH, E.UNPROCESSABLE)
ERROR_SCHEMA = "ErrorOut"


class ErrorCodeError(RuntimeError):
    pass


def declare(*codes: Code) -> dict[str, Any]:
    """The ``openapi_extra`` fragment naming a route handler's own codes."""
    return {"x-error-codes": sorted({c.code for c in codes})}


def implied(method: str, op: dict[str, Any]) -> set[Code]:
    """The codes an operation can answer because of its shape (module docstring)."""
    codes = set(EVERY)
    params = op.get("parameters", [])
    if params or "requestBody" in op:
        codes.add(E.VALIDATION_ERROR)
    if "requestBody" in op:
        codes.add(E.MALFORMED_BODY)
    if op.get("x-permission") != "public":
        codes.update(AUTHENTICATED)
        if method in UNSAFE:
            codes.add(E.CSRF)
    if any(p.get("in") == "query" and p.get("name") == "cursor" for p in params):
        codes.update(LIST)
    return codes


def complete(doc: dict[str, Any]) -> dict[str, Any]:
    """Write every operation's full ``x-error-codes`` and one response per error status."""
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    schemas[ERROR_SCHEMA] = {
        "type": "object", "title": ERROR_SCHEMA, "required": ["error"],
        "properties": {"error": {"type": "string", "title": "Error"},
                       "detail": {"title": "Detail"}},
        "additionalProperties": True,
    }  # fmt: skip
    for item in doc.get("paths", {}).values():
        for method, op in item.items():
            if not isinstance(op, dict) or "responses" not in op:
                continue
            unknown = [c for c in op.get("x-error-codes", []) if c not in REGISTRY]
            if unknown:
                raise ErrorCodeError(
                    f"{op.get('operationId')}: codes not in the registry {unknown}"
                )
            codes = {REGISTRY[c] for c in op.get("x-error-codes", [])} | implied(method, op)
            op["x-error-codes"] = sorted(c.code for c in codes)
            by_status: dict[int, list[str]] = {}
            for c in codes:
                by_status.setdefault(c.status, []).append(c.code)
            for status, names in by_status.items():
                response = op["responses"].setdefault(str(status), {})
                listed = ", ".join(sorted(names))
                response["description"] = f"error: {listed}"
                if (
                    str(status) != "422"
                ):  # 422 keeps FastAPI's validation schema: our body (safe_errors)
                    # is that shape plus "error", never with its optional input/ctx
                    response["content"] = {"application/json": {
                        "schema": {"$ref": f"#/components/schemas/{ERROR_SCHEMA}"}}}  # fmt: skip
    return doc
