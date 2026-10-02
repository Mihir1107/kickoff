"""Export file links: redirects, resolved addresses and pinned connections (ADR 0014 section 7)."""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable

import httpx
import pytest

from edisc_connector_slack_export.file_links import IPAddress, address_allowed, download
from edisc_connectors_base.types import FileUnavailableError, FileUnavailableReason

HOSTS = ("files.slack.test", "files-edge.slack.test")
PUBLIC = ipaddress.ip_address("93.184.215.14")
EDGE = ipaddress.ip_address("151.101.1.1")


class Dns:
    """Answers per host; a list of answers is consumed one per lookup (DNS rebinding)."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self.answers = answers
        self.lookups: list[str] = []

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        assert port == 443
        self.lookups.append(host)
        queue = self.answers.get(host)
        if not queue:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return [ipaddress.ip_address(a) for a in answer]


class Host:
    def __init__(self, routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.seen: list[tuple[str, str, str, str | None]] = []  # (dialled, Host, path, sni)

    def handler(self, request: httpx.Request) -> httpx.Response:
        sni = request.extensions.get("sni_hostname")
        assert request.headers["connection"] == "close"
        self.seen.append((request.url.host, request.headers["host"], request.url.path, sni))
        return self.routes[f"{request.headers['host']}{request.url.path}"](request)


def redirect(location: str, status: int = 302) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(status, headers={"location": location})


def ok(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(200, content=body)


async def fetch(host: Host, dns: Dns, link: str, *, max_redirects: int = 5) -> tuple[bytes, int]:
    tokens = 0

    async def take() -> None:
        nonlocal tokens
        tokens += 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(host.handler)) as http:
        body = b"".join(
            [
                chunk
                async for chunk in download(
                    http,
                    link,
                    "F1",
                    hosts=HOSTS,
                    max_redirects=max_redirects,
                    resolver=dns,
                    before_request=take,
                )
            ]
        )
    return body, tokens


async def refused(host: Host, dns: Dns, link: str, **kw: int) -> FileUnavailableError:
    with pytest.raises(FileUnavailableError) as info:
        await fetch(host, dns, link, **kw)
    assert "token" not in info.value.response.decode()  # the link (and its token) is never recorded
    return info.value


async def test_an_allowed_redirect_chain_is_followed_pinned_hop_by_hop() -> None:
    host = Host(
        {
            "files.slack.test/F1/a.txt": redirect("https://files-edge.slack.test/x/F1?token=t2"),
            "files-edge.slack.test/x/F1": redirect("/y/F1", 307),  # relative
            "files-edge.slack.test/y/F1": ok(b"payload"),
        }
    )
    dns = Dns({"files.slack.test": [[str(PUBLIC)]], "files-edge.slack.test": [[str(EDGE)]]})
    body, tokens = await fetch(host, dns, "https://files.slack.test/F1/a.txt?t=xoxe-token")
    assert body == b"payload"
    assert tokens == 3  # every hop takes a rate-limit token
    assert host.seen == [
        (str(PUBLIC), "files.slack.test", "/F1/a.txt", "files.slack.test"),
        (str(EDGE), "files-edge.slack.test", "/x/F1", "files-edge.slack.test"),
        (str(EDGE), "files-edge.slack.test", "/y/F1", "files-edge.slack.test"),
    ]


@pytest.mark.parametrize(
    "location",
    [
        "https://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "https://[::1]/",
        "http://files.slack.test/F1",  # allowed host, but not https
        "https://files.slack.test:8443/F1",  # allowed host, other port
        "https://slack.test/signin",  # Slack's login page for an expired token
    ],
)
async def test_a_redirect_off_the_allowlist_is_never_requested(location: str) -> None:
    host = Host({"files.slack.test/F1": redirect(location)})
    dns = Dns({"files.slack.test": [[str(PUBLIC)]]})
    gap = await refused(host, dns, "https://files.slack.test/F1?t=xoxe-token")
    assert gap.reason is FileUnavailableReason.EXPIRED_URL
    assert json.loads(gap.response) == {"ok": False, "error": "redirect_not_allowed", "hop": 1}
    assert len(host.seen) == 1


@pytest.mark.parametrize(
    "answer",
    [
        ["127.0.0.1"],
        ["93.184.215.14", "10.0.0.5"],  # one bad address in the answer is enough
        ["169.254.169.254"],
        ["::ffff:127.0.0.1"],
    ],
)
async def test_an_allowed_host_resolving_to_an_internal_address_is_refused(
    answer: list[str],
) -> None:
    host = Host({})
    dns = Dns({"files.slack.test": [answer]})
    gap = await refused(host, dns, "https://files.slack.test/F1?t=xoxe-token")
    assert gap.reason is FileUnavailableReason.EXTERNAL_OR_HIDDEN
    assert json.loads(gap.response)["error"] == "address_not_allowed"
    assert host.seen == []


async def test_dns_rebinding_between_hops_is_refused() -> None:
    host = Host({"files.slack.test/F1": redirect("/F1/again")})
    dns = Dns({"files.slack.test": [[str(PUBLIC)], ["127.0.0.1"]]})
    gap = await refused(host, dns, "https://files.slack.test/F1?t=xoxe-token")
    assert gap.reason is FileUnavailableReason.EXTERNAL_OR_HIDDEN
    assert [s[0] for s in host.seen] == [str(PUBLIC)]  # dialled the checked IP, then stopped


@pytest.mark.parametrize(
    ("link", "error"),
    [
        ("http://files.slack.test/F1", "file_host_not_allowed"),
        ("https://files.slack.test:444/F1", "file_host_not_allowed"),
        ("https://169.254.169.254/F1", "file_host_not_allowed"),
        ("https://evil.test/F1", "file_host_not_allowed"),
    ],
)
async def test_a_link_off_the_allowlist_is_never_resolved(link: str, error: str) -> None:
    host, dns = Host({}), Dns({})
    gap = await refused(host, dns, link)
    assert (gap.reason, json.loads(gap.response)["error"]) == (
        FileUnavailableReason.EXTERNAL_OR_HIDDEN,
        error,
    )
    assert dns.lookups == [] and host.seen == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (410, FileUnavailableReason.EXPIRED_URL),
        (401, FileUnavailableReason.PERMISSION),
        (403, FileUnavailableReason.PERMISSION),
        (404, FileUnavailableReason.DELETED),
        (500, FileUnavailableReason.UNREACHABLE),
        (304, FileUnavailableReason.UNREACHABLE),  # not a redirect: no Location semantics
    ],
)
async def test_statuses_map_to_gap_reasons(status: int, reason: FileUnavailableReason) -> None:
    host = Host({"files.slack.test/F1": lambda _: httpx.Response(status)})
    gap = await refused(
        host, Dns({"files.slack.test": [[str(PUBLIC)]]}), "https://files.slack.test/F1"
    )
    assert gap.reason is reason


async def test_redirect_limits_and_broken_redirects_are_unreachable() -> None:
    dns = Dns({"files.slack.test": [[str(PUBLIC)]]})
    loop = Host({"files.slack.test/F1": redirect("/F1")})
    gap = await refused(loop, dns, "https://files.slack.test/F1", max_redirects=2)
    assert (gap.reason, json.loads(gap.response)["error"]) == (
        FileUnavailableReason.UNREACHABLE,
        "too_many_redirects",
    )
    assert len(loop.seen) == 3
    bare = Host({"files.slack.test/F1": lambda _: httpx.Response(302)})
    gap = await refused(bare, dns, "https://files.slack.test/F1")
    assert gap.reason is FileUnavailableReason.UNREACHABLE


async def test_no_dns_answer_or_connection_is_unreachable() -> None:
    gap = await refused(Host({}), Dns({}), "https://files.slack.test/F1")
    assert (gap.reason, json.loads(gap.response)["error"]) == (
        FileUnavailableReason.UNREACHABLE,
        "gaierror",
    )

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    gap = await refused(
        Host({"files.slack.test/F1": down}),
        Dns({"files.slack.test": [[str(PUBLIC)]]}),
        "https://files.slack.test/F1",
    )
    assert (gap.reason, json.loads(gap.response)["error"]) == (
        FileUnavailableReason.UNREACHABLE,
        "ConnectError",
    )


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("93.184.215.14", True),
        ("2606:4700:4700::1111", True),
        ("::ffff:93.184.215.14", True),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("0.0.0.0", False),  # noqa: S104 (an address under test, not a bind)
        ("169.254.169.254", False),  # AWS/GCP/Azure metadata
        ("100.100.100.200", False),  # Alibaba metadata (shared address space)
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("fd00:ec2::254", False),  # AWS IPv6 metadata
        ("::ffff:127.0.0.1", False),
        ("::ffff:169.254.169.254", False),
        ("2002:7f00:1::1", False),  # 6to4 of 127.0.0.1
        ("ff02::1", False),
    ],
)
def test_address_allowed(address: str, allowed: bool) -> None:
    assert address_allowed(ipaddress.ip_address(address)) is allowed
