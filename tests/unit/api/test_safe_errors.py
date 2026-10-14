"""No error path echoes a request value (ADR 0016 amendment, D11): the sanitised 422 body, the
fixed 500, log lines without values, uvicorn's raw request line switched off. The integration test
``tests/integration/api/test_no_echo.py`` sends a canary into every route of the real API."""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_core import PydanticCustomError

from edisc_api.app import add_request_context
from edisc_api.errors import RecordedText
from edisc_api.safe_errors import (
    install,
    route_server_logs,
    safe_path,
    static_segments,
    validation_error_body,
)
from tests.logcapture import captured_logs


def canary() -> str:
    return f"Zq{secrets.token_hex(12)}"  # matches no redaction pattern: a leak would show


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[int] = Field(default_factory=list)


class ThingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=10)
    token: SecretStr
    n: int = 0
    when: str | None = None
    ident: uuid.UUID | None = None
    inner: Inner = Field(default_factory=Inner)
    tags: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> ThingIn:
        if self.name == "loud":
            raise ValueError(f"bad name {self.token.get_secret_value()}")  # a careless validator
        if self.name == "own":
            raise PydanticCustomError("edisc_own", "a static message")
        return self


router = APIRouter(prefix="/v1")


@router.post("/things/{thing_id}")
async def make_thing(
    thing_id: uuid.UUID,
    body: ThingIn,
    limit: Annotated[int, Query(ge=1, le=5)] = 1,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=8)] = None,
) -> dict[str, str]:
    return {"ok": "yes"}


@router.get("/boom")
async def boom(q: str) -> dict[str, str]:
    raise ValueError(f"cannot handle {q}")  # an unhandled error quoting the request


@router.get("/stream")
async def stream(q: str) -> StreamingResponse:
    async def body() -> AsyncIterator[bytes]:
        yield b"first chunk"
        raise RuntimeError(f"stored object differs ({q})")

    return StreamingResponse(body())


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    add_request_context(app)  # the production access line: the positive control of the log checks
    install(app, [router])
    return app


def _client(app: FastAPI, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, **kwargs), base_url="http://t.test"
    )


def _no(value: str, response: httpx.Response) -> None:
    assert value not in response.text, response.text
    assert value not in str(response.headers)


@pytest.mark.parametrize(
    "body",
    [
        lambda c: {"name": "a", "token": "t", "extra_secret": c},  # misplaced field
        lambda c: {"name": "a", c: "x", "token": "t"},  # the KEY is the value
        lambda c: {"name": "a", "token": [c]},  # wrong type
        lambda c: {"name": "a", "token": {c: c}},
        lambda c: {"name": c * 3, "token": "t"},  # over-long
        lambda c: {"name": "a", "token": "t", "n": c},
        lambda c: {"name": "a", "token": "t", "ident": c},  # uuid_parsing quotes a character
        lambda c: {"name": "a", "token": "t", "inner": {"items": [1, c]}},
        lambda c: {"name": "a", "token": "t", "inner": {c: 1}},
        lambda c: {"name": "a", "token": "t", "tags": {c: c}},  # dict key and value
        lambda c: {"name": "loud", "token": c},  # a validator quoting the value
        lambda c: c,  # the whole body
        lambda c: [c],
    ],
)
async def test_a_wrong_body_is_answered_without_its_values(body: Any) -> None:
    c = canary()
    async with _client(_app()) as http:
        with captured_logs() as logs:
            r = await http.post(f"/v1/things/{uuid.uuid4()}", json=body(c))
    assert r.status_code == 422
    _no(c, r)
    logs.require("request", method="POST", route="/v1/things/{thing_id}", status=422)
    assert c not in logs.getvalue()
    for error in r.json()["detail"]:
        assert set(error) == {"type", "loc", "msg"}  # never input, ctx or url


async def test_wrong_parameters_and_raw_bodies_are_answered_without_their_values() -> None:
    c = canary()
    async with _client(_app()) as http:
        with captured_logs() as logs:
            answers = [
                await http.post(f"/v1/things/{c}", json={"name": "a", "token": "t"}),
                await http.post(f"/v1/things/{uuid.uuid4()}?limit={c}", json={"name": "a", "token": "t"}),
                await http.post(f"/v1/things/{uuid.uuid4()}", json={"name": "a", "token": "t"},
                                headers={"Idempotency-Key": c}),
                await http.post(f"/v1/things/{uuid.uuid4()}", content=f'{{"name": "{c}'.encode(),
                                headers={"content-type": "application/json"}),
                await http.post(f"/v1/things/{uuid.uuid4()}", content=c.encode(),
                                headers={"content-type": "text/plain"}),
                await http.post(f"/v1/things/{uuid.uuid4()}", content=b"\xff" + c.encode(),
                                headers={"content-type": "application/json"}),
            ]  # fmt: skip
    # undecodable bytes: FastAPI's own 400 ("There was an error parsing the body")
    assert [r.status_code for r in answers] == [422] * 5 + [400]
    for r in answers:
        _no(c, r)
    logs.require("request", 5, method="POST", status=422)
    logs.require("request", method="POST", status=400)
    assert c not in logs.getvalue()


async def test_names_we_declare_stay_in_loc_and_client_chosen_ones_do_not() -> None:
    c = canary()
    async with _client(_app()) as http:
        r = await http.post(
            f"/v1/things/{uuid.uuid4()}",
            json={"name": "a", "token": "t", c: 1, "inner": {"items": ["x"]}, "tags": {c: "y"}},
        )
    locs = sorted(tuple(e["loc"]) for e in r.json()["detail"])
    assert locs == [("body", "*"), ("body", "inner", "items", 0), ("body", "tags", "*")]
    assert {e["type"] for e in r.json()["detail"]} == {"extra_forbidden", "int_parsing"}


async def test_messages_are_kept_only_when_the_schema_makes_them() -> None:
    async with _client(_app()) as http:
        r = await http.post(
            f"/v1/things/{uuid.uuid4()}", json={"name": "a" * 11, "token": "t", "ident": "nope"}
        )
        own = await http.post(f"/v1/things/{uuid.uuid4()}", json={"name": "own", "token": "t"})
        loud = await http.post(f"/v1/things/{uuid.uuid4()}", json={"name": "loud", "token": "t"})
    msgs = {e["type"]: e["msg"] for e in r.json()["detail"]}
    assert msgs == {
        "string_too_long": "String should have at most 10 characters",
        "uuid_parsing": "Input should be a valid UUID",
    }
    assert own.json()["detail"][0]["msg"] == "a static message"
    assert loud.json()["detail"][0] == {
        "type": "value_error",
        "loc": ["body"],
        "msg": "Invalid value",
    }


def test_the_body_never_carries_input_ctx_or_url() -> None:
    body = validation_error_body(
        [{"type": "value_error", "loc": ("body", "x"), "msg": "Value error, s3cr3t", "input": "s3cr3t",
          "ctx": {"error": ValueError("s3cr3t")}, "url": "https://errors.pydantic.dev"}],
        frozenset({"x"}),
    )  # fmt: skip
    assert "s3cr3t" not in json.dumps(body)
    assert body == {
        "error": "validation_error",
        "detail": [{"type": "value_error", "loc": ["body", "x"], "msg": "Invalid value"}],
    }


async def test_an_unhandled_error_is_a_fixed_500_and_a_log_line_without_its_message() -> None:
    c = canary()
    async with _client(_app()) as http:
        with captured_logs() as logs:
            r = await http.get("/v1/boom", params={"q": c})
    assert r.status_code == 500
    rid = r.headers["x-request-id"]
    assert r.json() == {"error": "internal_error", "detail": "internal error", "request_id": rid}
    _no(c, r)
    lines = [json.loads(line) for line in logs.getvalue().splitlines()]
    assert c not in logs.getvalue()
    (line,) = [x for x in lines if x["event"] == "unhandled error"]
    assert line["error_types"][0] == "builtins.ValueError"  # then the middleware's task group
    assert line["frames"][-1].endswith(" boom") and line["path"] == "/v1/boom"


async def test_an_error_after_the_first_byte_breaks_the_transfer_and_logs_no_message() -> None:
    c = canary()
    async with _client(_app()) as http:
        with captured_logs() as logs, pytest.raises(RuntimeError, match="stored object differs"):
            await http.get("/v1/stream", params={"q": c})
    assert c not in logs.getvalue()
    assert '"response_started": true' in logs.getvalue()


def test_log_paths_keep_only_static_route_segments() -> None:
    segments = static_segments(["/v1/things/{thing_id}", "/v1/jobs/{job_id}/cancel"])
    assert safe_path("/v1/things/Zq123/x", segments) == "/v1/things/*/*"
    assert safe_path("/v1/jobs/abc/cancel", segments) == "/v1/jobs/*/cancel"
    assert safe_path("/", segments) == "/"


def test_uvicorn_access_log_is_off_and_its_error_log_goes_through_ours() -> None:
    names = ("uvicorn", "uvicorn.error", "uvicorn.asgi", "uvicorn.access")
    saved = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).propagate,
                 logging.getLogger(n).disabled) for n in names}  # fmt: skip
    try:
        for n in names:
            logging.getLogger(n).addHandler(logging.StreamHandler())
            logging.getLogger(n).propagate = False
        route_server_logs()
        access = logging.getLogger("uvicorn.access")
        assert access.disabled and not access.handlers
        for n in ("uvicorn", "uvicorn.error"):
            assert logging.getLogger(n).propagate and not logging.getLogger(n).handlers
    finally:
        for n, (handlers, propagate, disabled) in saved.items():
            logger = logging.getLogger(n)
            logger.handlers[:] = handlers
            logger.propagate, logger.disabled = propagate, disabled


def test_recorded_worker_texts_pass_the_redactor_on_the_way_out() -> None:
    class Out(BaseModel):
        last_error: RecordedText | None

    leaked = "xoxb-1234567890-abcdefghij"
    assert (
        leaked not in Out(last_error=f"SlackApiError: invalid_auth for {leaked}").model_dump_json()
    )
    assert Out(last_error=None).last_error is None
