"""Every local test run leaves its full output on disk (`scripts/test_log.py`, CLAUDE.md "Commands")."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "test_log.py"

CHILD = r"""
import os, sys
sys.stdout.write("\x1b[31mred\x1b[0m line\n")
sys.stdout.flush()
sys.stderr.write("to stderr\n")
sys.stderr.flush()
print("artifacts=" + os.environ["EDISC_TEST_ARTIFACTS_DIR"])
print("timings=" + os.environ["EDISC_TEST_PHASE_TIMINGS_DIR"])
sys.exit(int(sys.argv[1]))
"""


def _run(tmp: Path, status: int, *extra: str, env: dict[str, str] | None = None) -> int:
    full = {**os.environ, "TEST_RUNS_DIR": str(tmp), **(env or {})}
    full.pop("EDISC_TEST_ARTIFACTS_DIR", None)
    full.pop("EDISC_TEST_PHASE_TIMINGS_DIR", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--label", "unit test", *extra, "--",
         sys.executable, "-c", CHILD, str(status)],
        env=full, capture_output=True, check=False,
    ).returncode  # fmt: skip


def _runs(tmp: Path) -> list[Path]:
    return sorted(p for p in tmp.iterdir() if p.is_dir() and not p.is_symlink())


def test_the_whole_output_is_saved_and_the_status_kept(tmp_path: Path) -> None:
    assert _run(tmp_path, 0) == 0
    (run,) = _runs(tmp_path)
    assert run.name.endswith("-unit_test")
    log = (run / "output.log").read_text()
    assert "red line\n" in log and "\x1b" not in log  # colour codes removed
    assert "to stderr\n" in log
    assert f"artifacts={run / 'artifacts'}" in log  # failure artifacts land next to the output
    assert f"timings={run / 'phase-timings'}" in log
    assert "exit status 0" in log
    assert (tmp_path / "latest").resolve() == run.resolve()


def test_a_failure_keeps_its_status_and_runs_the_failure_command(tmp_path: Path) -> None:
    assert _run(tmp_path, 3, "--on-failure", "echo stack logs here") == 3
    (run,) = _runs(tmp_path)
    assert "exit status 3" in (run / "output.log").read_text()
    assert (run / "on-failure.log").read_text() == "stack logs here\n"


def test_the_failure_command_does_not_run_on_success(tmp_path: Path) -> None:
    assert _run(tmp_path, 0, "--on-failure", "echo no") == 0
    (run,) = _runs(tmp_path)
    assert not (run / "on-failure.log").exists()


def test_old_runs_are_pruned(tmp_path: Path) -> None:
    for k in range(4):
        (tmp_path / f"2020010{k}T000000Z-old").mkdir()
    assert _run(tmp_path, 0, env={"TEST_RUNS_KEEP": "2"}) == 0
    names = [p.name for p in _runs(tmp_path)]
    assert (
        len(names) == 2 and names[0] == "20200103T000000Z-old" and names[1].endswith("-unit_test")
    )
