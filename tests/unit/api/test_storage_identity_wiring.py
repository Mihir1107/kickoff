"""The API's production wiring checks its storage identity before serving (M18 §3, T12):
``build_resources`` refuses foreign credentials and asks the store whether it is root."""

from __future__ import annotations

import ast
import inspect

from edisc_api import app


def _calls(fn: object) -> list[str]:
    tree = ast.parse(inspect.getsource(fn))  # type: ignore[arg-type]
    calls = sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Call)),
        key=lambda n: (n.lineno, n.col_offset),
    )  # source order (ast.walk is breadth-first)
    return [
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "") for n in calls
    ]


def test_build_resources_refuses_root_and_foreign_identities() -> None:
    calls = _calls(app.build_resources.__wrapped__)  # type: ignore[attr-defined]
    assert "refuse_foreign_identities" in calls
    assert "assert_least_privilege" in calls
    # the probe runs before anything is built on the client
    assert calls.index("assert_least_privilege") < calls.index("Resources")
