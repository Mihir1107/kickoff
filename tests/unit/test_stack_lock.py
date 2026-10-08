"""The exclusive edisc-test stack lock (`scripts/stack_lock.py`, ADR 0015 §24.6)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import scripts.stack_lock as sl


def test_acquire_then_a_second_acquire_fails_fast(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / "stack.lock"
    assert sl.acquire(lock, "test-env-up") == 0
    owner = json.loads(lock.read_text())
    assert owner["pid"] == os.getpid() and owner["label"] == "test-env-up"

    assert sl.acquire(lock, "test-integration") == 3  # a run already owns the stack
    err = capsys.readouterr().err
    assert "already in use" in err and "test-env-up" in err and "make test-env-down" in err
    assert json.loads(lock.read_text())["label"] == "test-env-up"  # never stolen


def test_a_crashed_runs_lock_is_not_auto_stolen(tmp_path: Path) -> None:
    """The marker is not keyed on a live pid: a leftover lock (even from a dead pid) still refuses,
    because auto-stealing is exactly how two runs would end up sharing a stack. It is cleared by
    test-env-down, not by a liveness guess."""
    lock = tmp_path / "stack.lock"
    lock.write_text(json.dumps({
        "pid": 1 << 22, "host": "whatever", "started": "2020-01-01T00:00:00", "label": "crashed",
    }))  # fmt: skip
    assert sl.acquire(lock, "fresh") == 3
    assert json.loads(lock.read_text())["label"] == "crashed"  # untouched


def test_an_unreadable_lock_still_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / "stack.lock"
    lock.write_text("not json")
    assert sl.acquire(lock, "fresh") == 3
    assert "another run" in capsys.readouterr().err


def test_release_is_idempotent_and_lets_a_new_run_acquire(tmp_path: Path) -> None:
    lock = tmp_path / "stack.lock"
    sl.acquire(lock, "a")
    assert sl.release(lock) == 0 and not lock.exists()
    assert sl.release(lock) == 0  # already gone
    assert sl.acquire(lock, "b") == 0


def test_require_fails_when_no_stack_is_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / "stack.lock"
    assert sl.require(lock) == 4
    assert "no edisc-test stack is up" in capsys.readouterr().err
    sl.acquire(lock, "a")
    assert sl.require(lock) == 0
