"""Downloading export file links without SSRF (ADR 0014 section 7).

Exports are attacker-influenced, so a link is fetched only if EVERY hop passes the same checks:

- **https only**, host in ``EDISC_EXPORT_FILE_HOSTS``, default port. A redirect is followed (up to
  ``EDISC_EXPORT_FILE_MAX_REDIRECTS``) only to a target that passes them too; a redirect anywhere else
  is never requested.
- **Resolved address:** the host is resolved once per hop and every address must be global unicast
  (no private, loopback, link-local, shared, reserved, multicast or unspecified ranges, which covers the
  cloud metadata endpoints; IPv4-mapped, 6to4 and Teredo addresses are checked as their IPv4 address).
- **Pinned connection:** the request goes to the validated IP, with the ``Host`` header and the TLS SNI
  (and so certificate verification) still on the hostname. A second DNS answer cannot redirect it
  (no DNS rebinding). Connections are not reused across hops or files (``Connection: close``).

Refusals become recorded file gaps (``FileUnavailableError``), never a stall:

- 410, or a redirect to a host or scheme that is not allowed (Slack sends an expired token to its login
  page): ``expired_url``.
- 401/403 ``permission``; 404 ``deleted``.
- An allowed host resolving to a disallowed address: ``external_or_hidden``, never requested.
- No DNS answer, no connection, a 3xx without ``Location``, too many redirects, other errors:
  ``unreachable``.

The URL never reaches a log or a gap record: bodies carry only a status or an error name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Collection

import httpx

from edisc_connectors_base.types import FileUnavailableError, FileUnavailableReason

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]

FILE_REFUSALS = {
    401: FileUnavailableReason.PERMISSION,  # revoked
    403: FileUnavailableReason.PERMISSION,
    404: FileUnavailableReason.DELETED,
    410: FileUnavailableReason.EXPIRED_URL,
}
REDIRECTS = frozenset({301, 302, 303, 307, 308})
CHUNK = 1 << 16


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


def address_allowed(ip: IPAddress) -> bool:
    """Global unicast only, judged on the embedded IPv4 address for mapped/6to4/Teredo IPv6."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = [ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())]
        if any(e is not None and not address_allowed(e) for e in embedded):
            return False
        if ip.ipv4_mapped is not None:
            return True
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified


def _gap(file_ref: str, reason: FileUnavailableReason, **detail: object) -> FileUnavailableError:
    return FileUnavailableError(file_ref, reason, json.dumps({"ok": False, **detail}).encode())


def _host_allowed(url: httpx.URL, hosts: Collection[str]) -> bool:
    return url.scheme == "https" and url.port is None and url.host in hosts


async def _pin(url: httpx.URL, resolver: Resolver, file_ref: str) -> IPAddress:
    try:
        addresses = await resolver(url.host, 443)
    except OSError as exc:
        raise _gap(file_ref, FileUnavailableReason.UNREACHABLE, error=type(exc).__name__) from None
    if not addresses:
        raise _gap(file_ref, FileUnavailableReason.UNREACHABLE, error="no_address")
    if not all(address_allowed(a) for a in addresses):
        raise _gap(file_ref, FileUnavailableReason.EXTERNAL_OR_HIDDEN, error="address_not_allowed")
    return addresses[0]


async def download(
    http: httpx.AsyncClient,
    link: str,
    file_ref: str,
    *,
    hosts: Collection[str],
    max_redirects: int,
    resolver: Resolver,
    before_request: Callable[[], Awaitable[object]],
) -> AsyncIterator[bytes]:
    """Stream a link's body; ``before_request`` runs before every hop (the rate-limit token)."""
    try:
        url = httpx.URL(link)
    except httpx.InvalidURL:
        raise _gap(file_ref, FileUnavailableReason.EXTERNAL_OR_HIDDEN, error="bad_link") from None
    if not _host_allowed(url, hosts):
        raise _gap(
            file_ref, FileUnavailableReason.EXTERNAL_OR_HIDDEN, error="file_host_not_allowed"
        )
    for hop in range(max_redirects + 1):
        ip = await _pin(url, resolver, file_ref)
        await before_request()
        request = http.build_request(
            "GET",
            url.copy_with(host=str(ip)),
            # pooled by origin = the IP: never reuse a connection whose certificate was checked for
            # another hostname on the same address
            headers={"Host": url.host, "Connection": "close"},
            extensions={"sni_hostname": url.host},
        )
        try:
            response = await http.send(request, stream=True, follow_redirects=False)
        except httpx.TransportError as exc:  # never logs the URL: only the error type
            raise _gap(
                file_ref, FileUnavailableReason.UNREACHABLE, error=type(exc).__name__
            ) from None
        try:
            location = response.headers.get("location")
            if response.status_code in REDIRECTS and location:
                try:
                    url = url.join(location)
                except httpx.InvalidURL:
                    raise _gap(
                        file_ref, FileUnavailableReason.EXPIRED_URL, error="redirect_not_allowed"
                    ) from None
                if not _host_allowed(url, hosts):
                    raise _gap(
                        file_ref,
                        FileUnavailableReason.EXPIRED_URL,
                        error="redirect_not_allowed",
                        hop=hop + 1,
                    )
                continue
            reason = FILE_REFUSALS.get(response.status_code)
            if reason is None and response.status_code >= 300:
                reason = FileUnavailableReason.UNREACHABLE
            if reason is not None:
                raise _gap(file_ref, reason, status=response.status_code)
            try:
                async for chunk in response.aiter_bytes(CHUNK):
                    yield chunk
            except httpx.TransportError as exc:
                raise _gap(
                    file_ref, FileUnavailableReason.UNREACHABLE, error=type(exc).__name__
                ) from None
            return
        finally:
            await response.aclose()
    raise _gap(file_ref, FileUnavailableReason.UNREACHABLE, error="too_many_redirects")
