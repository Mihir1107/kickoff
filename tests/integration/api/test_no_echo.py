"""A canary sent into every route and field, in wrong shapes and types, never comes back (ADR 0016
amendment, decision D11): not in a response body or header, not in a log line.

Generated from the app's own OpenAPI document, so a new route or field is covered without editing
this file. Each request carries ONE canary placement: a path parameter, a query parameter, a declared
header, a body field (wrong type, list, object, over-long; at every depth, array items included),
an extra body key, the key itself, the whole body, broken JSON, a non-JSON body, plus undeclared
headers the routes read themselves. The caller is a tenant admin, so validation is reached on every
route (authentication runs first). The handler-level refusals that quote nothing any more (unknown
source, unknown role, render time zone, export size) are driven with otherwise valid requests.
``X-Request-Id`` is the one header whose purpose is to be echoed (a client's correlation id).
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Iterator
from typing import Any

import httpx

from edisc_core.settings import Settings
from tests.integration.api.conftest import Api, TenantCtx, secret
from tests.logcapture import captured_logs

Placement = tuple[str, dict[str, Any]]  # (description, httpx request kwargs)


def _canary() -> str:
    return f"Zq{secrets.token_hex(12)}"  # matches no redaction pattern: a leak shows as is


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    for key in ("anyOf", "oneOf", "allOf"):
        options = [s for s in schema.get(key, ()) if s.get("type") != "null"]
        if options:
            return _resolve(spec, options[0])
    return schema


def _sample(schema: dict[str, Any]) -> Any:
    """A value of the right shape for a path parameter."""
    if schema.get("format") == "uuid":
        return str(uuid.uuid4())
    if schema.get("type") == "integer":
        return 1
    if schema.get("pattern") == "^[0-9a-f]{64}$":
        return "ab" * 32
    return "x"


def _wrong(schema: dict[str, Any], c: str) -> Iterator[tuple[str, Any]]:
    """Wrong-shaped values carrying the canary for a field of this schema."""
    kind = schema.get("type")
    if kind != "string":
        yield "string", c
    yield "list", [c]
    yield "object", {c: c}
    if kind == "string" and "maxLength" in schema:
        yield "too long", c * (schema["maxLength"] // len(c) + 2)
    if kind == "string" and ("enum" in schema or "format" in schema or "pattern" in schema):
        yield "bad value", c


def _fields(spec: dict[str, Any], schema: dict[str, Any], depth: int = 0) -> Iterator[
    tuple[list[str | int], dict[str, Any]]
]:  # fmt: skip
    """Every field path of a request schema (objects and array items, a few levels deep)."""
    schema = _resolve(spec, schema)
    if depth > 4:
        return
    if schema.get("type") == "object":
        for name, sub in schema.get("properties", {}).items():
            resolved = _resolve(spec, sub)
            yield [name], resolved
            for path, leaf in _fields(spec, resolved, depth + 1):
                yield [name, *path], leaf
    elif schema.get("type") == "array" and "items" in schema:
        for path, leaf in _fields(spec, schema["items"], depth + 1):
            yield [0, *path], leaf


def _place(path: list[str | int], value: Any) -> Any:
    if not path:
        return value
    head, rest = path[0], path[1:]
    return [_place(rest, value)] if isinstance(head, int) else {head: _place(rest, value)}


def _objects(spec: dict[str, Any], schema: dict[str, Any]) -> Iterator[list[str | int]]:
    """The path of every object level of a request body (where an extra key can go)."""
    schema = _resolve(spec, schema)
    if schema.get("type") == "object":
        yield []
    for path, leaf in _fields(spec, schema):
        if leaf.get("type") == "object":
            yield path


def placements(spec: dict[str, Any], c: str) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    """(method, url, description, request kwargs) for every canary placement of every route."""
    for template, item in spec["paths"].items():
        for method, op in item.items():
            params = op.get("parameters", [])
            valid = {p["name"]: _sample(_resolve(spec, p["schema"])) for p in params
                     if p["in"] == "path"}  # fmt: skip

            def url(**override: str) -> str:
                return template.format(**{**valid, **override})  # noqa: B023

            for p in params:
                where = p["in"]
                if where == "path":
                    yield method, url(**{p["name"]: c}), f"path {p['name']}", {}
                elif where == "query":
                    yield method, url(), f"query {p['name']}", {"params": {p["name"]: c}}
                    yield (
                        method,
                        url(),
                        f"query {p['name']} twice",
                        {"params": [(p["name"], c), (p["name"], c)]},
                    )
                elif where == "header":
                    yield method, url(), f"header {p['name']}", {"headers": {p["name"]: c}}
            yield method, url(), "undeclared headers", {"headers": {
                "content-digest": f"sha-256=:{c}:", "x-forwarded-for": c, "user-agent": c,
                "referer": f"https://{c}.test/", "origin": f"https://{c}.test", "cookie": f"{c}={c}",
                "accept-language": c,
            }}  # fmt: skip
            body = op.get("requestBody", {}).get("content", {})
            if "application/json" not in body:
                if body or method in ("put", "post"):
                    yield method, url(), "raw body", {"content": c.encode() * 3}
                continue
            schema = body["application/json"]["schema"]
            for path, leaf in _fields(spec, schema):
                for shape, value in _wrong(leaf, c):
                    yield method, url(), f"body {path} {shape}", {"json": _place(path, value)}
            for path in _objects(spec, schema):
                yield method, url(), f"body {path} extra key", {"json": _place(path, {"zz": c})}
                yield method, url(), f"body {path} key", {"json": _place(path, {c: 1})}
            for desc, kwargs in (
                ("whole body", {"json": c}),
                ("body list", {"json": [c]}),
                ("broken JSON", {"content": f'{{"a": "{c}'.encode(),
                                 "headers": {"content-type": "application/json"}}),
                ("text body", {"content": c.encode(), "headers": {"content-type": "text/plain"}}),
                ("invalid UTF-8", {"content": b'{"a": "\xff' + c.encode() + b'"}',
                                   "headers": {"content-type": "application/json"}}),
            ):  # fmt: skip
                yield method, url(), desc, kwargs


def _leaks(c: str, r: httpx.Response) -> bool:
    return c in r.text or c in str(r.headers)


async def test_a_canary_in_any_route_and_field_never_comes_back(
    api: Api, tenant: TenantCtx
) -> None:
    assert api.app is not None
    spec = await asyncio.to_thread(api.app.openapi)
    c = _canary()
    leaks: list[str] = []
    statuses: dict[int, int] = {}
    done = 0
    with captured_logs() as logs:
        async with api.client(tenant.subdomain, tenant.token(api.settings)) as http:
            for method, url, desc, kwargs in placements(spec, c):
                r = await http.request(method.upper(), url, **kwargs)
                done += 1
                statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
                wrong_shape = not desc.startswith(("query", "header", "undeclared"))
                if _leaks(c, r) or (wrong_shape and r.status_code < 400):
                    leaks.append(f"{method.upper()} {url} [{desc}] -> {r.status_code}")
    assert not leaks, "\n".join(leaks)
    logged = [line for line in logs.getvalue().splitlines() if c in line]
    assert not logged, "\n".join(logged[:10])
    assert done > 400 and len(spec["paths"]) >= 40  # the whole surface, not a few routes
    assert statuses.get(422, 0) > 300, statuses  # validation was reached, not stopped at auth
    assert statuses.get(500, 0) == 0, statuses  # a wrong shape is the client's error, never ours


async def test_a_canary_as_the_bearer_token_or_host_is_not_logged(
    api: Api, tenant: TenantCtx
) -> None:
    c = _canary()
    with captured_logs() as logs:
        async with api.client(tenant.subdomain, c) as http:
            r = await http.get(f"/v1/clients/{tenant.default_client_id}")
        async with api.client(c, tenant.token(api.settings)) as http:
            r2 = await http.get(f"/v1/clients/{tenant.default_client_id}")
    assert (r.status_code, r2.status_code) == (401, 401)
    assert not _leaks(c, r) and not _leaks(c, r2)
    assert c not in logs.getvalue()


async def test_handler_refusals_quote_nothing_the_request_sent(
    api: Api, tenant: TenantCtx, settings: Settings
) -> None:
    """Requests that pass validation and are refused by the route itself."""
    c = _canary()
    connection = {
        "source": c, "external_org_id": "T1", "config": {},
        "credentials": {"access_token": secret()},
    }  # fmt: skip
    assignment = {"principal_id": str(tenant.admin_id), "role": c, "scope_type": "tenant"}
    with captured_logs() as logs:
        async with api.client(tenant.subdomain, tenant.token(api.settings)) as http:
            answers = {
                "unknown source": await http.post(
                    f"/v1/clients/{tenant.default_client_id}/connections", json=connection
                ),
                "unknown role": await http.post("/v1/role-assignments", json=assignment),
                "render time zone": await http.post(
                    f"/v1/jobs/{uuid.uuid4()}/renders", json={"time_zone": f"Mars/{c}"}
                ),
                "cursor": await http.get(
                    f"/v1/clients/{tenant.default_client_id}/connections", params={"cursor": c}
                ),
            }
    assert {k: r.status_code for k, r in answers.items()} == dict.fromkeys(answers, 422)
    assert answers["unknown source"].json()["detail"] == "unknown source"
    assert answers["unknown role"].json()["detail"] == [
        {"type": "edisc_unknown_role", "loc": ["body"], "msg": "unknown role"}
    ]
    assert answers["render time zone"].json()["detail"] == (
        "unknown time zone (IANA names of the pinned tzdata)"
    )
    assert not [k for k, r in answers.items() if _leaks(c, r)]
    assert c not in logs.getvalue()
