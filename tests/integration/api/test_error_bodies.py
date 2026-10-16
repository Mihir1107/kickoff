"""Real error bodies validate against the COMMITTED document (`docs/api/openapi.json`).

The test client's check (`error_check.py`) validates every error answer of every API test against
the document generated from the code; this test reads the published file itself, so the contract a
client generates its types from is what the API sends (422: `ValidationErrorOut` or
`UnprocessableOut`, never FastAPI's `HTTPValidationError` with `input`/`ctx`)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from .conftest import Api, TenantCtx

DOC = json.loads((Path(__file__).parents[3] / "docs/api/openapi.json").read_text())
MISSING = str(uuid.UUID(int=1))


def _validate(path: str, method: str, status: int, body: Any) -> None:
    op = DOC["paths"][path][method]
    schema = op["responses"][str(status)]["content"]["application/json"]["schema"]
    jsonschema.validate({**schema, "components": DOC["components"]}, body)
    if body["error"] == "validation_error":
        strict = {"$ref": "#/components/schemas/ValidationErrorOut"}
        jsonschema.validate({**strict, "components": DOC["components"]}, body)


@pytest.mark.parametrize(
    ("method", "url", "body", "template", "code"),
    [
        ("get", f"/v1/evidence/{MISSING}/content", None, "/v1/evidence/{evidence_id}/content",
         "validation_error"),  # a required query parameter missing
        ("get", "/v1/evidence/not-a-uuid/content?purpose=preview", None,
         "/v1/evidence/{evidence_id}/content", "validation_error"),
        ("post", "/v1/clients", {"name": 5, "extra": True}, "/v1/clients", "validation_error"),
        ("get", "/v1/jobs?limit=0", None, "/v1/jobs", "validation_error"),
        ("get", "/v1/jobs?cursor=not-a-cursor", None, "/v1/jobs", "unprocessable"),
    ],
)  # fmt: skip
async def test_a_real_422_body_matches_the_published_document(
    api: Api, tenant: TenantCtx, method: str, url: str, body: Any, template: str, code: str
) -> None:
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        r = await c.request(method.upper(), url, json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"] == code
    _validate(template, method, 422, r.json())
    if code == "validation_error":
        assert r.json()["detail"], "the issues are listed"
