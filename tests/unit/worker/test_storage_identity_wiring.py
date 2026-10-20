"""Every worker process checks its storage identity at startup (M18 §3)."""

from __future__ import annotations

import ast
import inspect

from edisc_worker import __main__ as worker_main


def _calls(fn: object) -> list[str]:
    tree = ast.parse(inspect.getsource(fn))  # type: ignore[arg-type]
    calls = sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Call)),
        key=lambda n: (n.lineno, n.col_offset),
    )  # source order (ast.walk is breadth-first)
    return [
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "") for n in calls
    ]


def test_the_general_worker_refuses_root_and_foreign_identities() -> None:
    calls = _calls(worker_main.activities_for.__wrapped__)  # type: ignore[attr-defined]
    assert "refuse_foreign_identities" in calls and "assert_least_privilege" in calls
