from __future__ import annotations

from edisc_api.client_ip import client_ip

LB = ["10.0.0.0/24"]


def test_direct_callers_cannot_choose_their_address() -> None:
    assert client_ip("203.0.113.9", "1.2.3.4", LB) == "203.0.113.9"
    assert client_ip("203.0.113.9", "1.2.3.4", []) == "203.0.113.9"


def test_behind_a_trusted_load_balancer_the_forwarded_client_is_used() -> None:
    assert client_ip("10.0.0.5", "198.51.100.7", LB) == "198.51.100.7"
    # a client-supplied prefix is ignored: the right-most untrusted hop is the one our LB saw
    assert client_ip("10.0.0.5", "1.2.3.4, 198.51.100.7", LB) == "198.51.100.7"
    # several of our proxies in the chain are skipped
    assert client_ip("10.0.0.5", "198.51.100.7, 10.0.0.9", LB) == "198.51.100.7"


def test_garbage_or_missing_headers_fall_back_to_the_peer() -> None:
    assert client_ip("10.0.0.5", None, LB) == "10.0.0.5"
    assert client_ip("10.0.0.5", "not-an-ip", LB) == "10.0.0.5"
    assert client_ip(None, None, LB) == "unknown"
