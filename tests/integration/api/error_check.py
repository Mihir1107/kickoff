"""Every error answer's code is declared for the operation that answered it (`edisc_api.error_codes`).

The API test client (`conftest.Api.client`) runs :class:`ErrorCodeCheck` on every response: a 4xx
or 5xx answer of a documented operation must carry a code (``{"error": ...}``, or the sign-in bounce
page's ``auth_error=``), the code must be in that operation's ``x-error-codes`` and its status must
be the registry's. A failure fails the test that made the request, so the published document
cannot drift from what the routes answer. A path or method no operation documents is not checked
(Starlette answers it, not a route)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI

from edisc_api.error_codes import REGISTRY, complete

BOUNCE = re.compile(r"auth_error=([a-z_]+)")
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


@dataclass
class ErrorCodeCheck:
    operations: list[tuple[str, re.Pattern[str], dict[str, Any]]]

    @classmethod
    def for_app(cls, app: FastAPI) -> ErrorCodeCheck:
        doc = complete(app.openapi())
        ops = []
        for path, item in doc["paths"].items():
            pattern = re.compile(
                "^"
                + re.sub(
                    r"\{[^/}]+\}", "[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}")
                )
                + "$"
            )
            for method, op in item.items():
                if method in METHODS:
                    ops.append((method.upper(), pattern, op))
        # static segments win over templates (a literal path before a parameterised one)
        ops.sort(key=lambda o: o[1].pattern.count("[^/]+"))
        return cls(ops)

    def operation(self, method: str, path: str) -> dict[str, Any] | None:
        return next((op for m, p, op in self.operations if m == method and p.match(path)), None)

    async def __call__(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        op = self.operation(response.request.method, response.request.url.path)
        if op is None:
            return
        body = (await response.aread()).decode(errors="replace")
        code = _code(response.headers.get("content-type", ""), body)
        where = f"{response.request.method} {response.request.url.path} ({op.get('operationId')})"
        assert code is not None, (
            f"{where} answered {response.status_code} without an error code: {body[:300]!r}"
        )
        declared = op.get("x-error-codes", [])
        assert code in declared, (
            f"{where} answered {code!r}, which it does not declare (x-error-codes {declared}):"
            " add it to the route's perm(...) (edisc_api.error_codes)"
        )
        known = REGISTRY[code]
        assert known.status == response.status_code, (
            f"{where} answered {code!r} with {response.status_code}; the registry says {known.status}"
        )


def _code(content_type: str, body: str) -> str | None:
    if content_type.startswith("application/json"):
        try:
            data = json.loads(body)
        except ValueError:
            return None
        code = data.get("error") if isinstance(data, dict) else None
        return code if isinstance(code, str) else None
    if content_type.startswith("text/html"):
        found = BOUNCE.search(body)
        return found.group(1) if found else None
    return None
