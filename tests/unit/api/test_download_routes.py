"""Every content route is cut when its session ends (M17 step 5b, plan §1.5).

A content route is one that anchors its read before the first byte (CLAUDE.md: every route that
returns evidence bytes calls ``audit.anchor_now`` before the response starts). Each must pass its
body through ``edisc_api.downloads.guarded`` and have a cut test naming its operationId; a new
content route fails here until it does both."""

from __future__ import annotations

import ast
from pathlib import Path

from edisc_api import routes

ROUTES = Path(routes.__file__).parent
TESTS = Path(__file__).parents[2] / "integration" / "api"
# operationId -> the integration test file whose cut test names it (check_cut(..., "<id>"))
CUT_TESTS = {
    "evidence_content": "test_content_reads_anchored.py",
    "render_file_content": "test_content_reads_anchored.py",
    "render_native_content": "test_render_natives.py",
    "render_package": "test_render_packages.py",
    "job_package": "test_job_package.py",
    "report_file_content": "test_reports.py",
    "report_package": "test_reports.py",
    "preview": "test_preview.py",
}


def _calls(fn: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


def _routes() -> dict[str, set[str]]:
    found = {}
    for path in sorted(ROUTES.glob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.AsyncFunctionDef) and any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post")
                for d in node.decorator_list
            ):
                found[node.name] = _calls(node)
    return found


def test_every_content_route_is_guarded_and_has_a_cut_test() -> None:
    content = {name for name, calls in _routes().items() if "anchor_now" in calls}
    assert content == set(CUT_TESTS), (
        f"content routes {sorted(content)}: each needs guarded(...) and a cut test (CUT_TESTS)"
    )
    for name, calls in _routes().items():
        if name in content:
            assert "guarded" in calls, f"{name} streams without edisc_api.downloads.guarded"
    for name, file in CUT_TESTS.items():
        assert f'"{name}"' in (TESTS / file).read_text(), f"no cut test names {name} in {file}"
