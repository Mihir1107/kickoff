"""FastAPI application (M13): wiring, error rendering, the authenticated-caller dependency."""

from __future__ import annotations

import asyncio
import contextlib
import time
import urllib.parse
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
from edisc_api.client_ip import client_ip
from edisc_api.errors import ApiError
from edisc_api.oidc import OidcClient
from edisc_api.pagination import bind_cursors
from edisc_api.safe_errors import install as install_safe_errors
from edisc_api.safe_errors import request_path
from edisc_api.sessions import SESSION_COOKIE, Keyring
from edisc_connector_dummy.connector import DummyConnector
from edisc_connector_dummy.guard import dummy_permitted, ensure_dummy_permitted
from edisc_connector_slack_export.connector import SlackExportConnector
from edisc_connectors_base.protocol import Connector
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_core.envelope import SecretBox
from edisc_core.kms import LocalKmsClient
from edisc_core.logs import bind_context, clear_context, get_logger
from edisc_core.settings import Settings
from edisc_db.session import create_engine, session_factory, warm_pool
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
    oidc: OidcClient | None = (
        None  # browser sign-in (default: over the authenticator's HTTP client)
    )
    keyring: Keyring | None = None  # CSRF and PKCE keys (default: from settings)

    def __post_init__(self) -> None:
        ensure_dummy_permitted(self.settings.env, self.connectors)
        if self.keyring is None:  # eager: the API refuses to start without keys (fail closed)
            self.keyring = Keyring.from_settings(self.settings)

    @property
    def sign_in(self) -> OidcClient:
        if self.oidc is None:  # over the authenticator's HTTP client and JWKS cache
            self.oidc = OidcClient(
                self.settings, self.authenticator.jwks.http, self.authenticator.jwks
            )
        return self.oidc

    @property
    def keys(self) -> Keyring:
        if self.keyring is None:
            raise RuntimeError("Resources.keyring is set in __post_init__")
        return self.keyring


@contextlib.asynccontextmanager
async def build_resources(settings: Settings) -> AsyncIterator[Resources]:
    """Production wiring (tests build ``Resources`` from their own fixtures)."""
    engine = create_engine(settings, "app")
    redis = aioredis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=5)
    http = httpx.AsyncClient()
    try:
        await warm_pool(engine)
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
                {
                    **({"dummy": DummyConnector(limiter)} if dummy_permitted(settings.env) else {}),
                    "slack_export": SlackExportConnector(sessions, s3, settings, limiter, http),
                },
                redis,
            )
    finally:
        await http.aclose()
        await redis.aclose()
        await engine.dispose()


def warm_routes(app: FastAPI) -> int:
    """Build every route's dependency and validation models now. FastAPI (0.142) builds them lazily,
    under a thread lock, on the first request that matches a router: about 350 ms of CPU on the
    event loop of the API process (found by the loop guard, ADR 0015 §24). Run it in a thread at
    startup. Returns how many routes were built."""
    from fastapi.routing import _IncludedRouter

    built, todo = 0, list(app.router.routes)
    while todo:
        route = todo.pop()
        if isinstance(route, _IncludedRouter):
            for child in (*route.effective_candidates(), *route.effective_low_priority_routes()):
                if isinstance(child, _IncludedRouter):
                    todo.append(child)
                else:
                    built += 1
    return built


def add_request_context(app: FastAPI) -> None:
    """Every request gets an id (client-supplied X-Request-Id if sane), bound to all its log lines
    and returned in the response; routes put it in custody and audit payloads. Writes the access
    line (uvicorn's own prints the raw request line: switched off in main)."""

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Any) -> Any:
        rid = request_id(request)
        request.state.request_id = rid
        clear_context()
        path = request_path(request)  # never a client-chosen segment, never the query string
        bind_context(request_id=rid, path=path)
        started = time.monotonic()
        try:
            response = await call_next(request)
            route = request.scope.get("route")
            log.info(
                "request", method=request.method, route=getattr(route, "path", None),
                status=response.status_code, ms=round((time.monotonic() - started) * 1000, 1),
            )  # fmt: skip
        finally:
            clear_context()
        response.headers["x-request-id"] = rid
        if SESSION_COOKIE in request.cookies:  # never cached, never shared (ADR 0016 §2)
            response.headers["cache-control"] = "no-store"
            vary = response.headers.get("vary")
            response.headers["vary"] = f"{vary}, Cookie" if vary else "Cookie"
        return response


def create_app(settings: Settings, resources: Resources | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(warm_routes, app)  # never on the loop at the first requests
        if resources is not None:
            app.state.resources = resources
            yield
            return
        async with build_resources(settings) as built:
            app.state.resources = built
            yield

    # every list route's cursor is bound to its route, path and filters (pagination.bind_cursors)
    app = FastAPI(
        title="eDiscovery collection API", version="1", lifespan=lifespan,
        dependencies=[Depends(bind_cursors)],
    )  # fmt: skip
    if resources is not None:
        app.state.resources = resources

    add_request_context(app)

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        # same response for unknown tenant and any token problem: no subdomain enumeration
        log.info("authentication failed", reason=str(exc), path=request_path(request))
        await count_auth_failure(request)
        return JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.code, "detail": exc.detail, **exc.extra}, status_code=exc.status
        )

    from edisc_api.routes import register

    install_safe_errors(app, register(app))  # last: its middleware wraps every other one
    return app


def resources(request: Request) -> Resources:
    res: Resources = request.app.state.resources
    return res


def _failure_key(request: Request) -> str:
    res: Resources = request.app.state.resources
    client = client_ip(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        res.settings.api_trusted_proxies,
    )
    return f"edisc:authfail:{request.headers.get('host', '')}:{client}"


async def count_auth_failure(request: Request) -> None:
    res: Resources | None = getattr(request.app.state, "resources", None)
    if res is None or res.redis is None:
        return
    key = _failure_key(request)
    count = await res.redis.incr(key)
    if count == 1:
        await res.redis.expire(key, 60)


async def check_auth_throttle(request: Request) -> None:
    """429 when this (host, address) failed authentication too often in the last minute."""
    res = resources(request)
    if res.redis is not None:
        failures = await res.redis.get(_failure_key(request))
        if failures is not None and int(failures) >= res.settings.api_auth_failures_per_minute:
            raise ApiError(429, "too_many_requests", "too many failed authentication attempts")


async def caller(request: Request) -> Caller:
    """The authenticated caller. Too many recent failures from this address for this host: 429 before
    any token or tenant lookup (slows credential stuffing and subdomain probing)."""
    res = resources(request)
    await check_auth_throttle(request)
    host, authorization = request.headers.get("host"), request.headers.get("authorization")
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id is not None and authorization is not None:  # before either is evaluated
        raise ApiError(
            400, "ambiguous_credentials", "send the session cookie or a bearer token, not both"
        )
    if session_id is None:
        return await res.authenticator.authenticate(host, authorization)
    # the cookie is ambient: only requests the tenant's own pages make (or the user typed) may use it
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise ApiError(403, "cross_site", "cookie-authenticated requests must be same-origin")
    caller = await res.authenticator.authenticate_session(
        host, session_id, touch=not getattr(request.state, "passive", False)
    )
    if request.method not in SAFE_METHODS:
        if not res.keys.csrf_valid(session_id, request.headers.get("x-csrf-token")):
            raise ApiError(403, "csrf", "missing or invalid X-CSRF-Token")
        if request_origin(request) != tenant_origin(res.settings, host):
            raise ApiError(403, "csrf", "Origin is not this tenant")
    return caller


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def tenant_origin(settings: Settings, host: str | None) -> str:
    """The origin of the tenant host the request reached (the host was matched to the tenant)."""
    return f"{settings.api_public_scheme}://{(host or '').lower()}"


def request_origin(request: Request) -> str | None:
    """``Origin``, else the origin of ``Referer`` (ADR 0016 §3); None when neither is usable."""
    origin = request.headers.get("origin")
    if origin is not None:
        return origin.lower()
    referer = request.headers.get("referer")
    if not referer:
        return None
    parts = urllib.parse.urlsplit(referer)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else None


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
