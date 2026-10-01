"""FastAPI application (M13): wiring, error rendering, the authenticated-caller dependency."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from types_aiobotocore_s3 import S3Client

from edisc_api.auth import Authenticator, AuthError, Caller, JwksCache
from edisc_api.errors import ApiError
from edisc_connector_dummy.connector import DummyConnector
from edisc_connectors_base.protocol import Connector
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.envelope import SecretBox
from edisc_core.kms import LocalKmsClient
from edisc_core.logs import bind_context, clear_context, get_logger
from edisc_core.settings import Settings
from edisc_db.session import create_engine, session_factory
from edisc_evidence.s3 import s3_client

log = get_logger(__name__)


@dataclass
class Resources:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    temporal: Client
    limiter: RateLimiter
    box: SecretBox
    authenticator: Authenticator
    connectors: Mapping[str, Connector]
    redis: aioredis.Redis | None = None  # auth-failure throttling (None: not throttled, tests only)


@contextlib.asynccontextmanager
async def build_resources(settings: Settings) -> AsyncIterator[Resources]:
    """Production wiring (tests build ``Resources`` from their own fixtures)."""
    engine = create_engine(settings, "app")
    redis = aioredis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=5)
    http = httpx.AsyncClient()
    try:
        sessions = session_factory(engine)
        temporal = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        limiter = RateLimiter(redis, settings.rate_limits)
        async with s3_client(settings) as s3:
            yield Resources(
                settings,
                sessions,
                s3,
                temporal,
                limiter,
                SecretBox(LocalKmsClient(settings)),
                Authenticator(settings, sessions, JwksCache(settings, http)),
                {"dummy": DummyConnector(limiter)},
                redis,
            )
    finally:
        await http.aclose()
        await redis.aclose()
        await engine.dispose()


def create_app(settings: Settings, resources: Resources | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if resources is not None:
            app.state.resources = resources
            yield
            return
        async with build_resources(settings) as built:
            app.state.resources = built
            yield

    app = FastAPI(title="eDiscovery collection API", version="1", lifespan=lifespan)
    if resources is not None:
        app.state.resources = resources

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Any) -> Any:
        """Every request gets an id (client-supplied X-Request-Id if sane), bound to all its log lines
        and returned in the response; routes put it in custody and audit payloads."""
        rid = request_id(request)
        request.state.request_id = rid
        clear_context()
        bind_context(request_id=rid, path=request.url.path)
        try:
            response = await call_next(request)
        finally:
            clear_context()
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        # same response for unknown tenant and any token problem: no subdomain enumeration
        log.info("authentication failed", reason=str(exc), path=request.url.path)
        await _count_auth_failure(request)
        return JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "detail": exc.detail}, status_code=exc.status)

    from edisc_api.routes import register

    register(app)
    return app


def resources(request: Request) -> Resources:
    res: Resources = request.app.state.resources
    return res


def _failure_key(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    return f"edisc:authfail:{request.headers.get('host', '')}:{client}"


async def _count_auth_failure(request: Request) -> None:
    res: Resources | None = getattr(request.app.state, "resources", None)
    if res is None or res.redis is None:
        return
    key = _failure_key(request)
    count = await res.redis.incr(key)
    if count == 1:
        await res.redis.expire(key, 60)


async def caller(request: Request) -> Caller:
    """The authenticated caller. Too many recent failures from this address for this host: 429 before
    any token or tenant lookup (slows credential stuffing and subdomain probing)."""
    res = resources(request)
    if res.redis is not None:
        failures = await res.redis.get(_failure_key(request))
        if failures is not None and int(failures) >= res.settings.api_auth_failures_per_minute:
            raise ApiError(429, "too_many_requests", "too many failed authentication attempts")
    return await res.authenticator.authenticate(
        request.headers.get("host"), request.headers.get("authorization")
    )


CallerDep = Annotated[Caller, Depends(caller)]
ResourcesDep = Annotated[Resources, Depends(resources)]


def request_id(request: Request) -> str:
    existing: str | None = getattr(request.state, "request_id", None)
    if existing:
        return existing
    rid = request.headers.get("x-request-id")
    if rid and len(rid) <= 128 and rid.replace("-", "").replace("_", "").isalnum():
        return rid
    return str(uuid.uuid4())


RequestIdDep = Annotated[str, Depends(request_id)]
