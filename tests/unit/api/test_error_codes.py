"""The error-code registry, the declarations and the test-time check (`edisc_api.error_codes`,
`tests/integration/api/error_check.py`). The check is driven here with constructed responses, so a
broken check fails these tests whatever the routes happen to answer."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx
import pytest
from scripts.api_compat import compare

from edisc_api import openapi_doc
from edisc_api.app import create_app
from edisc_api.error_codes import REGISTRY, ErrorCodeError, complete, implied
from edisc_core.settings import Environment, Settings
from tests.integration.api.error_check import ErrorCodeCheck

DOC = openapi_doc.document()


@pytest.fixture(scope="module")
def check() -> ErrorCodeCheck:
    return ErrorCodeCheck.for_app(create_app(Settings.model_construct(env=Environment.TEST)))


def _response(method: str, path: str, status: int, body: Any, html: bool = False) -> httpx.Response:
    request = httpx.Request(method, f"http://t.edisc.localhost{path}")
    if html:
        return httpx.Response(status, request=request, html=body)
    return httpx.Response(status, request=request, json=body)


def _run(check: ErrorCodeCheck, response: httpx.Response) -> None:
    asyncio.run(check(response))


def test_a_declared_code_with_its_status_passes(check: ErrorCodeCheck) -> None:
    _run(check, _response("GET", "/v1/jobs/0190", 404, {"error": "not_found"}))
    _run(check, _response("GET", "/v1/jobs/0190", 401, {"error": "unauthorized"}))  # implied
    _run(check, _response("GET", "/v1/auth/callback", 403,
                          '<meta http-equiv="refresh" content="0;url=/?auth_error=groups_overage">',
                          html=True))  # fmt: skip
    _run(check, _response("GET", "/v1/jobs/0190", 200, {"id": "x"}))
    _run(check, _response("GET", "/no/such/path", 404, {"error": "not_found"}))  # no operation


def test_an_undeclared_code_fails(check: ErrorCodeCheck) -> None:
    with pytest.raises(AssertionError, match="does not declare"):
        _run(check, _response("GET", "/v1/jobs/0190", 409, {"error": "already_revoked"}))
    with pytest.raises(AssertionError, match="does not declare"):
        _run(check, _response("GET", "/v1/auth/callback", 400,
                              '<meta http-equiv="refresh" content="0;url=/?auth_error=invalid_return_to">',
                              html=True))  # fmt: skip


def test_a_code_with_another_status_fails(check: ErrorCodeCheck) -> None:
    with pytest.raises(AssertionError, match="the registry says 404"):
        _run(check, _response("GET", "/v1/jobs/0190", 410, {"error": "not_found"}))


def test_an_error_without_a_code_fails(check: ErrorCodeCheck) -> None:
    with pytest.raises(AssertionError, match="without an error code"):
        _run(check, _response("GET", "/v1/jobs/0190", 400, {"detail": "Not a code"}))
    with pytest.raises(AssertionError, match="without an error code"):
        _run(check, _response("GET", "/v1/jobs/0190", 500, "<p>oops</p>", html=True))


def test_static_paths_win_over_templates(check: ErrorCodeCheck) -> None:
    op = check.operation("GET", "/v1/me/permissions")
    assert op is not None and op["operationId"] == "my_permissions"


def test_every_route_declares_its_codes_from_the_registry() -> None:
    raw = create_app(Settings.model_construct(env=Environment.PRODUCTION)).openapi()
    for path, item in raw["paths"].items():
        for method, op in item.items():
            assert "x-error-codes" in op, f"{method.upper()} {path} declares no error codes"
            assert set(op["x-error-codes"]) <= set(REGISTRY), op["operationId"]
    for item in DOC["paths"].values():
        for method, op in item.items():
            codes = set(op["x-error-codes"])
            assert {c.code for c in implied(method, op)} <= codes, op["operationId"]
            for status in {REGISTRY[c].status for c in codes}:
                assert str(status) in op["responses"], (op["operationId"], status)


def test_an_unknown_code_fails_the_document() -> None:
    doc = copy.deepcopy(DOC)
    next(iter(doc["paths"]["/v1/me"].values()))["x-error-codes"] = ["no_such_code"]
    with pytest.raises(ErrorCodeError, match="no_such_code"):
        complete(doc)


def test_removing_a_declared_code_from_the_published_document_is_breaking() -> None:
    """The compat rule `edisc-error-code-removed` on REAL input: the published document."""
    new = copy.deepcopy(DOC)
    op = new["paths"]["/v1/jobs/{job_id}"]["get"]
    assert "not_found" in op["x-error-codes"]
    op["x-error-codes"] = [c for c in op["x-error-codes"] if c != "not_found"]
    verdict = compare(DOC, new, [])
    assert [(f.id, f.operation_id) for f in verdict.breaking] == [
        ("edisc-error-code-removed", "get_job")
    ], json.dumps([f.text for f in verdict.breaking])
