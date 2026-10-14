"""The sign-in in real browsers (ADR 0016 A1; M17 step 2): Chromium, Firefox and WebKit.

The IdP's redirect back is a cross-site navigation, so a ``SameSite=Strict`` cookie set by the
callback is not sent on that chain's next hop: a 303 to ``return_to`` would load signed out. The
callback answers a bounce page instead, whose meta refresh starts a NEW same-site navigation. This is
browser behaviour, so it is tested in browsers, over real HTTPS:

- one TLS server (uvicorn, a throwaway self-signed certificate; the browser context ignores
  certificate errors) serves the API for the tenant host ``<sub>.edisc-browser.test`` and the IdP's
  sign-in page for ``idp.example.test`` (another SITE), dispatching on the Host header;
- every browser is pointed at a small CONNECT proxy that tunnels any host to that server, so real
  redirects, real cookie jars and real SameSite decisions happen in the browser;
- the IdP page holds a link back (a person clicking "continue" at a real IdP).

The control at the end proves the harness enforces SameSite: a cross-site navigation straight from
the IdP page to the tenant arrives WITHOUT the session cookie.

The ``__Host-`` prefix is tested the same way: a neighbouring tenant's subdomain and the parent
domain never receive the session cookie (a ``Domain`` cookie, the control, does reach them), and a
same-site request back from the neighbour carries it and is refused (ADR 0016 §2, step-2 note 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import html
import socket
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from playwright.async_api import async_playwright

from edisc_api.app import create_app
from edisc_api.auth import Authenticator
from edisc_api.sessions import SESSION_COOKIE

from .conftest import Api, TenantCtx
from .oidc import TEST_IDP
from .sessions_flow import enable_login

BASE = "edisc-browser.test"
IDP = "idp.example.test"
Scope = MutableMapping[str, Any]
ASGI = Callable[
    [Scope, Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "edisc browser test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(f"*.{BASE}"), x509.DNSName(IDP)]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))  # fmt: skip
    return cert_path, key_path


@contextlib.asynccontextmanager
async def _connect_proxy(target_port: int) -> AsyncIterator[int]:
    """An HTTP CONNECT proxy that tunnels EVERY host to 127.0.0.1:target_port."""

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        writer.close()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        if not head.startswith(b"CONNECT "):
            writer.write(b"HTTP/1.1 400 Bad Request\r\ncontent-length: 0\r\n\r\n")
            writer.close()
            return
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", target_port)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()


def _dispatch(api_app: ASGI, idp_app: ASGI) -> ASGI:
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        host = dict(scope.get("headers") or []).get(b"host", b"").decode().split(":")[0]
        await (idp_app if host == IDP else api_app)(scope, receive, send)

    return app


def _idp_page(tenant_host: str, subject: str) -> ASGI:
    async def app(scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        url = f"https://{IDP}{scope['path']}?{scope['query_string'].decode()}"
        path, query = TEST_IDP.authorize(url, subject=subject)
        back = html.escape(
            f"https://{tenant_host}{path}?{urllib.parse.urlencode(query)}", quote=True
        )
        body = (f'<!doctype html><a id="continue" href="{back}">continue</a>'
                f'<a id="straight" href="https://{tenant_host}/v1/me">to the tenant</a>').encode()  # fmt: skip
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html")]})  # fmt: skip
        await send({"type": "http.response.body", "body": body})

    return app


@dataclasses.dataclass
class _Seen:
    """One request as the server got it."""

    host: str
    path: str
    cookie: bool  # the session cookie was sent
    fetch_site: str | None
    status: int | None = None


@dataclasses.dataclass
class _Harness:
    host: str
    seen: list[_Seen]
    context: Any
    page: Any

    def at(self, host: str) -> list[_Seen]:
        return [r for r in self.seen if r.host == host]


@contextlib.asynccontextmanager
async def _signed_in_browser(
    api: Api, tenant: TenantCtx, engine: str, tmp_path: Path
) -> AsyncIterator[_Harness]:
    """A browser through the CONNECT proxy, signed in on the tenant host through the real flow
    (the bounce page). Every request the server gets is recorded, whatever its host."""
    await enable_login(api, tenant)
    TEST_IDP.authorize_base = f"https://{IDP}"
    settings = api.settings.model_copy(update={"api_base_domain": BASE})
    resources = dataclasses.replace(
        api.resources, settings=settings, oidc=None, keyring=None,
        authenticator=Authenticator(settings, api.sessions, api.resources.authenticator.jwks),
    )  # fmt: skip
    host = f"{tenant.subdomain}.{BASE}"
    seen: list[_Seen] = []
    api_app = create_app(settings, resources)

    async def recording(scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] == "/favicon.ico":  # Firefox asks for one
            await api_app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        site = headers.get(b"sec-fetch-site")
        row = _Seen(headers.get(b"host", b"").decode().split(":")[0], scope["path"],
                    f"{SESSION_COOKIE}=".encode() in headers.get(b"cookie", b""),
                    site.decode() if site else None)  # fmt: skip
        seen.append(row)

        async def sending(message: Any) -> None:
            if message["type"] == "http.response.start":
                row.status = message["status"]
            await send(message)

        await api_app(scope, receive, sending)

    cert, key = _certificate(tmp_path)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        _dispatch(recording, _idp_page(host, tenant.admin_subject)), host="127.0.0.1", port=port,
        ssl_certfile=str(cert), ssl_keyfile=str(key), log_level="warning", lifespan="off",
    ))  # fmt: skip
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 (uvicorn exposes a flag, no event)
            await asyncio.sleep(0.05)
        async with _connect_proxy(port) as proxy_port, async_playwright() as pw:
            browser = await getattr(pw, engine).launch(
                proxy={"server": f"http://127.0.0.1:{proxy_port}"}
            )
            try:
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                # the UI sends the person to sign in (a real 303 to the IdP); they come back to /v1/me
                await page.goto(f"https://{host}/v1/auth/login?return_to=/v1/me")
                assert urllib.parse.urlsplit(page.url).netloc == IDP
                async with page.expect_navigation(url=f"https://{host}/v1/me"):
                    await page.click("#continue")
                yield _Harness(host, seen, context, page)
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await serving


@pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
async def test_the_first_page_after_the_bounce_is_signed_in(
    api: Api, tenant: TenantCtx, engine: str, tmp_path: Path
) -> None:
    async with _signed_in_browser(api, tenant, engine, tmp_path) as b:
        host, page = b.host, b.page
        body = await page.inner_text("body")
        assert str(tenant.admin_id) in body, body  # signed in on the first page
        paths = [(r.path, r.cookie) for r in b.seen]
        assert ("/v1/auth/callback", False) in paths  # Strict cookie not on the callback
        assert paths[-1] == ("/v1/me", True)  # ... but on the bounce's navigation
        cookies = {c["name"]: c for c in await b.context.cookies(f"https://{host}")}
        session = cookies["__Host-edisc_session"]
        assert session["httpOnly"] and session["secure"] and session["sameSite"] == "Strict"
        assert "__Host-edisc_login" not in cookies

        # control: from the IdP's page straight to the tenant (cross-site): no cookie sent
        await page.goto(f"https://{IDP}/authorize?{_dummy_authorize(host)}")
        await page.click("#straight")
        await page.wait_for_url(f"https://{host}/v1/me")
        assert (b.seen[-1].path, b.seen[-1].cookie) == ("/v1/me", False), b.seen[-3:]
        assert "unauthorized" in await page.inner_text("body")


@pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
async def test_the_session_cookie_never_reaches_another_subdomain(
    api: Api, tenant: TenantCtx, engine: str, tmp_path: Path
) -> None:
    """``__Host-``: Secure, ``Path=/``, no ``Domain``, so the browser keeps the cookie to exactly
    the tenant's host. A neighbouring tenant's subdomain and the parent domain never receive it,
    neither by a typed address nor by a same-site link. Second layer: a request a neighbouring
    subdomain's page sends back to the tenant IS same-site (the Strict cookie goes with it), and the
    API refuses it (403 ``cross_site``, ADR 0016 step-2 note 3)."""
    async with _signed_in_browser(api, tenant, engine, tmp_path) as b:
        host, page = b.host, b.page
        assert b.seen[-1].path == "/v1/me" and b.seen[-1].cookie  # signed in on the tenant host
        neighbour, parent = f"neighbour-{tenant.subdomain}.{BASE}", BASE
        # control: a cookie WITH Domain (what __Host- forbids) does reach the neighbour, so an
        # absent session cookie below is the browser's host-only rule, not the harness
        await b.context.add_cookies([{"name": "domain_wide", "value": "1", "domain": f".{BASE}",
                                      "path": "/", "secure": True, "sameSite": "Lax"}])  # fmt: skip

        await page.goto(f"https://{neighbour}/v1/me")  # typed (Sec-Fetch-Site: none)
        await page.goto(f"https://{parent}/v1/me")
        await page.goto(f"https://{host}/v1/me")  # a same-site link from the tenant's page
        await page.evaluate(f"""() => {{ const a = document.createElement("a");
            a.href = "https://{neighbour}/v1/me?via=link"; document.body.append(a); a.click(); }}""")
        await page.wait_for_url(f"https://{neighbour}/v1/me?via=link")
        visits = b.at(neighbour) + b.at(parent)
        assert len(b.at(neighbour)) == 2 and len(b.at(parent)) == 1, visits
        assert not any(r.cookie for r in visits), visits
        domain_wide = await page.evaluate("() => document.cookie")
        assert "domain_wide=1" in domain_wide  # the control cookie WAS delivered to the neighbour
        assert "edisc_session" not in domain_wide  # httpOnly: never readable by any page
        jar = {c["name"]: c for c in await b.context.cookies(f"https://{host}")}
        session = jar[SESSION_COOKIE]
        assert SESSION_COOKIE.startswith("__Host-")
        assert session["domain"] == host and session["path"] == "/"  # host-only (no Domain)

        # second layer: the neighbour's page calls the tenant with credentials (same-site)
        before = len(b.at(host))
        await page.evaluate(f"""() => fetch("https://{host}/v1/me",
            {{credentials: "include", mode: "no-cors"}}).catch(() => null)""")
        for _ in range(100):
            if len(b.at(host)) > before and b.at(host)[-1].status is not None:
                break
            await asyncio.sleep(0.05)
        call = b.at(host)[-1]
        assert call.fetch_site == "same-site" and call.cookie, call  # the Strict cookie went along
        assert call.status == 403, call  # ... and the API refused it


def _dummy_authorize(host: str) -> str:
    """A well-formed authorize query, so the test IdP renders its page (the grant goes unused)."""
    return urllib.parse.urlencode({
        "response_type": "code", "client_id": "edisc-browser", "scope": "openid",
        "redirect_uri": f"https://{host}/v1/auth/callback", "state": "s", "nonce": "n",
        "code_challenge": "c", "code_challenge_method": "S256",
    })  # fmt: skip
