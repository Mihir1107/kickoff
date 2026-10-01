"""The client address for throttling and logs, safe behind proxies.

``X-Forwarded-For`` is attacker-controlled unless it was written by our own proxies. It is used only
when the socket peer is in ``EDISC_API_TRUSTED_PROXIES``. Then the chain is read from the right,
skipping trusted proxies, and the first untrusted hop is the client. Otherwise the socket peer is the
client and any ``X-Forwarded-For`` is ignored (a direct caller cannot pick its own identity).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from functools import lru_cache

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=32)
def _networks(cidrs: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(c, strict=False) for c in cidrs)


def _trusted(address: str, networks: Sequence[IPNetwork]) -> bool:
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return False
    return any(ip in n for n in networks)


def client_ip(peer: str | None, forwarded_for: str | None, trusted_proxies: Sequence[str]) -> str:
    peer = peer or "unknown"
    networks = _networks(tuple(trusted_proxies))
    if not forwarded_for or not _trusted(peer, networks):
        return peer
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _trusted(hop, networks):
            try:
                return str(ipaddress.ip_address(hop))
            except ValueError:
                return peer  # garbage from a client behind the proxy: do not trust it either
    return hops[0] if hops else peer  # every hop is one of our proxies
