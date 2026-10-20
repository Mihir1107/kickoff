"""Every worker process checks its storage identity at startup, and ``--holds`` runs nothing but
the holds queue (M18 §3: a process loads only its own identity)."""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys

import pytest

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


def test_the_holds_worker_checks_both_of_its_identities() -> None:
    calls = _calls(worker_main.run_holds)
    assert calls.count("assert_least_privilege") == 2 and "refuse_foreign_identities" in calls


@pytest.mark.parametrize("other", [["--maintenance"], ["--exports"], ["--renders"], ["--reports"],
                                   ["--source", "dummy"], ["--queue", "x"]])  # fmt: skip
def test_holds_refuses_every_other_queue(other: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "edisc_worker", "--holds", *other],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert "--holds runs no other queue" in proc.stderr
