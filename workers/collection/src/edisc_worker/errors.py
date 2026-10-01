"""Error classification for activities (ADR 0012 section 3).

Activities never let a raw exception reach a workflow: ``classify`` maps it to one of the classes below
and ``to_application_error`` wraps it in a Temporal ``ApplicationError`` whose ``type`` is the class.
Workflows branch only on that type. The message carries the exception type and text (secrets are
redacted by the logging layer; connector exceptions never contain tokens).

Pure module: no Temporal client, DB or network imports, so it is unit-testable and sandbox-safe.
"""

from __future__ import annotations

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from botocore.exceptions import ConnectionError as BotoConnectionError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from temporalio.exceptions import ApplicationError

from edisc_connectors_base.ratelimit import SourceThrottledError
from edisc_connectors_base.types import (
    AuthenticationError,
    InvalidCursorError,
    SourceUnavailableError,
)
from edisc_core.canonical import CanonicalizationError
from edisc_custody.chain import PayloadError
from edisc_db.session import is_retryable_db_error, sqlstate_of
from edisc_evidence.worm import WormConflictError
from edisc_evidence.writer import (
    ContentLockTimeoutError,
    EvidenceCopyTimeoutError,
    EvidenceIntegrityError,
)
from edisc_normalizer.slack import NormalizationError
from edisc_worker.contracts import NON_RETRYABLE, ErrorClass

JOB_CLOSED_SQLSTATE = (
    "EA005"  # guard_job_open(): insert for a terminal or sealed job (migration 0012)
)


_S3_TRANSIENT_CODES = frozenset(
    {
        "SlowDown",
        "InternalError",
        "ServiceUnavailable",
        "RequestTimeout",
        "RequestTimeTooSkewed",
        "XMinioServerNotInitialized",
    }
)


def classify(exc: BaseException) -> ErrorClass:
    """The class of an exception raised inside an activity. Order matters: the most specific first."""
    if isinstance(exc, AuthenticationError):
        return ErrorClass.AUTH_REQUIRED
    if sqlstate_of(exc) == JOB_CLOSED_SQLSTATE:
        return ErrorClass.JOB_CLOSED
    # custody / WORM anchor integrity concerns the whole job's record
    if isinstance(exc, WormConflictError | PayloadError | CanonicalizationError):
        return ErrorClass.JOB_INTEGRITY
    if isinstance(exc, NormalizationError | InvalidCursorError | EvidenceIntegrityError):
        return ErrorClass.UNIT_INTEGRITY
    if isinstance(exc, ContentLockTimeoutError | EvidenceCopyTimeoutError):
        return ErrorClass.TRANSIENT  # TimeoutError subclasses, named for clarity
    if is_retryable_db_error(exc):
        return ErrorClass.TRANSIENT
    if isinstance(
        exc,
        TimeoutError
        | ConnectionError
        | SourceUnavailableError
        | SourceThrottledError
        | RedisConnectionError
        | RedisTimeoutError
        | BotoConnectionError
        | EndpointConnectionError
        | ReadTimeoutError,
    ):
        return ErrorClass.TRANSIENT
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        code = exc.response.get("Error", {}).get("Code", "")
        if status >= 500 or code in _S3_TRANSIENT_CODES:
            return ErrorClass.TRANSIENT
    return ErrorClass.UNCLASSIFIED


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:4000]


def to_application_error(
    exc: BaseException, *, error_class: ErrorClass | None = None, final: bool = False
) -> ApplicationError:
    """``final`` makes a retryable class non-retryable (its own attempt budget is used up)."""
    cls = error_class or classify(exc)
    return ApplicationError(
        describe(exc),
        type(exc).__name__,
        type=cls.value,
        non_retryable=final or cls in NON_RETRYABLE,
    )
