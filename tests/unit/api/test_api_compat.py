"""The API compatibility check (`scripts/api_compat.py`, M17 step 4, plan §5, threat T26): a
fixture spec pair per rule of the plan, run through the PINNED oasdiff and our own rules. Each pair
changes one thing in `data/compat_base.json`; the expected finding ids are what the check must
report (oasdiff's ids where it rates the change ERR, ours where it is silent: the module docstring
lists why)."""

from __future__ import annotations

import copy
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from scripts import api_compat
from scripts.api_compat import CompatError, compare, judge, load_allowlist, our_findings

from edisc_api import openapi_doc

BASE: dict[str, Any] = json.loads((Path(__file__).parent / "data" / "compat_base.json").read_text())


def _s(d: dict[str, Any]) -> dict[str, Any]:
    schemas: dict[str, Any] = d["components"]["schemas"]
    return schemas


def _get(d: dict[str, Any]) -> dict[str, Any]:
    op: dict[str, Any] = d["paths"]["/v1/things/{thing_id}"]["get"]
    return op


def _post(d: dict[str, Any]) -> None:
    d["paths"]["/v1/things"]["get"] = d["paths"]["/v1/things"].pop("post")


Edit = Callable[[dict[str, Any]], object]
BREAKING: dict[str, tuple[Edit, str]] = {
    "removed_path": (lambda d: d["paths"].pop("/v1/things"), "api-path-removed-without-deprecation"),
    "removed_operation": (_post, "api-removed-without-deprecation"),
    "removed_response_status": (lambda d: _get(d)["responses"].pop("404"), "edisc-response-status-removed"),
    "removed_response_field": (lambda d: _s(d)["Thing"]["properties"].pop("note"), "edisc-property-removed"),
    "response_enum_value_removed": (
        lambda d: _s(d)["Thing"]["properties"]["status"].update(enum=["active"]), "edisc-enum-value-removed"),
    "new_required_request_field": (
        lambda d: (_s(d)["ThingIn"]["properties"].update(extra={"type": "string"}),
                   _s(d)["ThingIn"]["required"].append("extra")), "new-required-request-property"),
    "new_required_parameter": (
        lambda d: _get(d)["parameters"].append(
            {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}),
        "new-required-request-parameter"),
    "request_field_made_required": (
        lambda d: _s(d)["ThingIn"]["required"].append("tag"), "request-property-became-required"),
    "request_enum_value_removed": (
        lambda d: _s(d)["ThingIn"]["properties"]["kind"].update(enum=["x"]),
        "request-property-enum-value-removed"),
    "type_changed": (
        lambda d: _s(d)["ThingIn"]["properties"]["name"].update(type="integer"),
        "request-property-type-changed"),
    "request_nullable_to_non_nullable": (
        lambda d: _s(d)["ThingIn"]["properties"]["size"].update(type="integer"),
        "request-property-became-not-nullable"),
    "max_length_lowered": (
        lambda d: _s(d)["ThingIn"]["properties"]["name"].update(maxLength=50),
        "request-property-max-length-decreased"),
    "pattern_added": (
        lambda d: _s(d)["ThingIn"]["properties"]["tag"].update(pattern="^[a-z]+$"),
        "request-property-pattern-added"),
    "minimum_raised": (
        lambda d: _get(d)["parameters"][1]["schema"].update(minimum=5), "request-parameter-min-increased"),
    "parameter_default_changed": (
        lambda d: _get(d)["parameters"][1]["schema"].update(default=20), "edisc-default-changed"),
    "body_default_changed": (
        lambda d: _s(d)["ThingIn"]["properties"]["mode"].update(default="slow"), "edisc-default-changed"),
    "response_field_became_optional": (
        lambda d: _s(d)["Thing"]["required"].remove("count"), "response-property-became-optional"),
    "response_field_became_nullable": (
        lambda d: _s(d)["Thing"]["properties"]["count"].update(type=["integer", "null"]),
        "response-property-became-nullable"),
    "operation_id_changed": (lambda d: _get(d).update(operationId="fetch_thing"), "edisc-operation-id-changed"),
    "media_type_changed": (
        lambda d: _get(d)["responses"]["200"].update(
            content={"application/x-thing+json": _get(d)["responses"]["200"]["content"]["application/json"]}),
        "edisc-media-type-removed"),
    "x_permission_changed": (
        lambda d: _get(d).update({"x-permission": "thing.admin"}), "edisc-x-permission-changed"),
    "error_code_removed": (
        lambda d: _get(d).update({"x-error-codes": ["not_found"]}), "edisc-error-code-removed"),
    "sse_event_schema_changed": (
        lambda d: _s(d)["ThingEvent"]["properties"].update(extra={"type": "string"}),
        "edisc-sse-event-changed"),
    "sse_event_removed": (
        lambda d: d["paths"]["/v1/things/{thing_id}/stream"]["get"].update({"x-sse-events": []}),
        "edisc-sse-event-changed"),
}  # fmt: skip
NOT_BREAKING: dict[str, Edit] = {
    "new_path": lambda d: d["paths"].update({"/v1/other": {"get": {
        "operationId": "other", "x-permission": "p", "responses": {"200": {"description": "ok"}}}}}),
    "new_optional_request_field": lambda d: _s(d)["ThingIn"]["properties"].update(opt={"type": "string"}),
    "new_response_field": lambda d: _s(d)["Thing"]["properties"].update(more={"type": "string"}),
    "new_response_enum_value": lambda d: _s(d)["Thing"]["properties"]["status"].update(
        enum=["active", "done", "paused"]),
}  # fmt: skip


def _changed(edit: Edit) -> dict[str, Any]:
    new = copy.deepcopy(BASE)
    edit(new)
    return new


@pytest.mark.parametrize("case", sorted(BREAKING))
def test_each_breaking_change_fails_the_check(case: str) -> None:
    edit, rule = BREAKING[case]
    verdict = compare(BASE, _changed(edit), [])
    assert not verdict.ok
    assert rule in {f.id for f in verdict.breaking}, [f.id for f in verdict.breaking]


@pytest.mark.parametrize("case", sorted(NOT_BREAKING))
def test_additive_changes_pass(case: str) -> None:
    verdict = compare(BASE, _changed(NOT_BREAKING[case]), [])
    assert verdict.ok and verdict.breaking == [], [f.id for f in verdict.breaking]


def test_a_new_response_enum_value_is_a_warning_in_the_summary() -> None:
    verdict = compare(BASE, _changed(NOT_BREAKING["new_response_enum_value"]), [])
    assert {f.id for f in verdict.warnings} == {"response-property-enum-value-added"}
    assert "response-property-enum-value-added" in api_compat.summary(verdict)


def test_identical_specs_have_no_finding() -> None:
    real = openapi_doc.document()
    verdict = compare(real, copy.deepcopy(real), [])
    assert verdict.ok and not verdict.warnings and our_findings(real, real) == []


# ------------------------------------------------------------------ the allow-list
ENTRY = {
    "rule": "edisc-x-permission-changed", "operation": "get_thing",
    "reason": "the read moved to a narrower permission", "frontend_commit": "0a1b2c3d",
}  # fmt: skip


def _allow(tmp_path: Path, entries: object) -> list[dict[str, str]]:
    path = tmp_path / "allow.yaml"
    path.write_text(json.dumps(entries))  # JSON is YAML
    return load_allowlist(path)


def test_an_allowed_break_passes_and_is_listed(tmp_path: Path) -> None:
    verdict = compare(
        BASE, _changed(BREAKING["x_permission_changed"][0]), _allow(tmp_path, [ENTRY])
    )
    assert verdict.ok and [f.id for f in verdict.allowed] == ["edisc-x-permission-changed"]
    by_route = {**ENTRY, "operation": "GET /v1/things/{thing_id}"}
    assert compare(BASE, _changed(BREAKING["x_permission_changed"][0]),
                   _allow(tmp_path, [by_route])).ok  # fmt: skip


def test_an_entry_for_another_operation_or_rule_allows_nothing(tmp_path: Path) -> None:
    other = {**ENTRY, "operation": "create_thing"}
    verdict = compare(
        BASE, _changed(BREAKING["x_permission_changed"][0]), _allow(tmp_path, [other])
    )
    assert not verdict.ok and verdict.unused == [other]


def test_an_entry_matching_nothing_fails(tmp_path: Path) -> None:
    verdict = judge([], _allow(tmp_path, [ENTRY]))
    assert not verdict.ok and verdict.unused == [ENTRY]


@pytest.mark.parametrize("missing", ["rule", "operation", "reason", "frontend_commit"])
def test_an_entry_without_all_four_fields_fails(tmp_path: Path, missing: str) -> None:
    with pytest.raises(CompatError, match=missing):
        _allow(tmp_path, [{k: v for k, v in ENTRY.items() if k != missing}])
    with pytest.raises(CompatError, match=missing):
        _allow(tmp_path, [{**ENTRY, missing: "  "}])


def test_the_frontend_commit_must_be_a_commit_id(tmp_path: Path) -> None:
    with pytest.raises(CompatError, match="commit id"):
        _allow(tmp_path, [{**ENTRY, "frontend_commit": "see the PR"}])


def test_the_committed_allowlist_is_valid() -> None:
    load_allowlist(api_compat.ALLOWLIST)


# ------------------------------------------------------------------ the published document
def test_the_committed_openapi_document_matches_the_code() -> None:
    """The drift check: regenerate and compare byte for byte (`make openapi` rewrites it)."""
    committed = openapi_doc.DOCUMENT.read_text()
    assert committed == openapi_doc.render(openapi_doc.document()), (
        "docs/api/openapi.json is out of date: run `make openapi` and commit it"
    )


def test_the_document_does_not_depend_on_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    first = openapi_doc.render(openapi_doc.document())
    monkeypatch.setenv("EDISC_ENV", "test")
    monkeypatch.setenv("EDISC_API_BASE_DOMAIN", "elsewhere.example")
    monkeypatch.setenv("EDISC_ENV_FILE", "/nonexistent")
    assert openapi_doc.render(openapi_doc.document()) == first


def test_every_operation_has_a_unique_function_name_and_a_permission() -> None:
    doc = openapi_doc.document()
    ops = openapi_doc.operations(doc)
    ids = [op["operationId"] for _, _, op in ops]
    assert len(ids) == len(set(ids)) and all("_v1_" not in i for i in ids)
    assert all("x-permission" in op for _, _, op in ops)
    assert "servers" not in doc


def test_the_check_command_fails_on_a_stale_document(tmp_path: Path) -> None:
    stale = tmp_path / "openapi.json"
    stale.write_text(
        openapi_doc.DOCUMENT.read_text().replace('"version": "1"', '"version": "0"', 1)
    )
    assert openapi_doc.main(["--check", "--out", str(stale)]) == 1
    assert openapi_doc.main(["--check", "--out", str(tmp_path / "missing.json")]) == 1
    assert openapi_doc.main(["--out", str(stale)]) == 0  # regenerated
    assert openapi_doc.main(["--check", "--out", str(stale)]) == 0


def test_duplicate_operation_ids_and_undeclared_permissions_are_refused() -> None:
    doc = copy.deepcopy(BASE)
    doc["paths"]["/v1/things"]["post"]["operationId"] = "get_thing"
    with pytest.raises(openapi_doc.OpenApiError, match="unique"):
        openapi_doc.check(doc)
    doc = copy.deepcopy(BASE)
    del doc["paths"]["/v1/things"]["post"]["x-permission"]
    with pytest.raises(openapi_doc.OpenApiError, match="x-permission"):
        openapi_doc.check(doc)


def test_an_oasdiff_download_with_another_checksum_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download that is not the pinned archive (a tampered release, a proxy's error page) is
    refused before anything is extracted or kept."""
    monkeypatch.setenv("EDISC_TOOL_CACHE", str(tmp_path))  # nothing cached: it downloads

    class Fake(io.BytesIO):
        def __enter__(self) -> Fake:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    seen: list[str] = []

    def urlopen(url: str, timeout: float) -> Fake:
        seen.append(url)
        return Fake(b"not the release archive")

    monkeypatch.setattr(api_compat.urllib.request, "urlopen", urlopen)
    with pytest.raises(CompatError, match="not the pinned one"):
        api_compat.oasdiff_binary()
    assert seen and seen[0].startswith("https://github.com/oasdiff/oasdiff/releases/download/v")
    assert not list(tmp_path.rglob("oasdiff"))  # nothing unverified was kept
