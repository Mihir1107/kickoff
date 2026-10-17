"""The production-readiness gate (`scripts/production_readiness.py`): a break the frontend has not
absorbed (an allow-list entry with `status: pending_frontend`) refuses production. It fails the CI
job `production-readiness` (blocking on release tags) and the `report` (production) target of the
report image, whose `readiness` stage runs it."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from scripts import api_compat, production_readiness

ROOT = Path(__file__).parents[2]
ABSORBED = {
    "rule": "edisc-x-permission-changed", "operation": "get_thing",
    "reason": "the read moved to a narrower permission", "frontend_commit": "0a1b2c3d",
}  # fmt: skip
PENDING = {k: v for k, v in ABSORBED.items() if k != "frontend_commit"} | {
    "status": "pending_frontend", "tracking": "docs/REQS.md#thing-permission",
}  # fmt: skip


def _list(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    path = tmp_path / "allow.yaml"
    path.write_text(json.dumps(entries))
    return path


def test_a_pending_entry_refuses_production(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        production_readiness.main(["--allowlist", str(_list(tmp_path, [ABSORBED, PENDING]))]) == 1
    )
    out = capsys.readouterr().out
    assert "NOT READY FOR PRODUCTION" in out and "get_thing" in out and PENDING["tracking"] in out


def test_absorbed_entries_and_an_empty_list_are_ready(tmp_path: Path) -> None:
    assert production_readiness.main(["--allowlist", str(_list(tmp_path, [ABSORBED]))]) == 0
    assert production_readiness.main(["--allowlist", str(_list(tmp_path, []))]) == 0


def test_a_malformed_list_is_never_ready(tmp_path: Path) -> None:
    with pytest.raises(api_compat.CompatError):
        production_readiness.main(["--allowlist", str(_list(tmp_path, [{"rule": "x"}]))])


def test_the_committed_list_decides_as_a_file_run(tmp_path: Path) -> None:
    """Run as a file, the way the image's readiness stage runs it (scripts/ on sys.path, not the
    repository root): the verdict is the committed list's."""
    expected = (
        1
        if api_compat.pending_frontend(api_compat.load_allowlist(ROOT / api_compat.ALLOWLIST))
        else 0
    )
    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "production_readiness.py"),
         "--allowlist", str(ROOT / api_compat.ALLOWLIST)],
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )  # fmt: skip
    assert done.returncode == expected, done.stdout + done.stderr


def _stages(dockerfile: str) -> dict[str, tuple[str, str]]:
    """stage name -> (parent, body)"""
    stages: dict[str, tuple[str, str]] = {}
    parts = re.split(r"^FROM ", dockerfile, flags=re.M)[1:]
    for part in parts:
        head, _, body = part.partition("\n")
        m = re.match(r"(?:--platform=\S+\s+)?(\S+)\s+AS\s+(\S+)", head)
        assert m, head
        stages[m.group(2)] = (m.group(1), body)
    return stages


def test_the_production_image_cannot_be_built_without_the_gate() -> None:
    stages = _stages((ROOT / "deploy" / "report-image" / "Dockerfile").read_text())
    parent, body = stages["readiness"]
    assert "scripts/production_readiness.py" in body and "RUN " in body
    assert "COPY docs/api/breaking-allowlist.yaml" in body
    parent, body = stages["report"]
    assert "COPY --from=readiness" in body, "the production target must depend on the gate"
    # the test image and the runtime layers must not depend on the production target (CI builds
    # report-test while the frontend has not caught up)
    assert stages["report-test"][0] != "report" and stages["runtime"][0] != "report"
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "!docs/api/breaking-allowlist.yaml" in ignore


def test_ci_runs_the_gate_and_it_blocks_release_tags() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    job = workflow["jobs"]["production-readiness"]
    assert any(step.get("run") == "make production-readiness" for step in job["steps"])
    expression = str(job["continue-on-error"])
    m = re.fullmatch(r"\$\{\{ !startsWith\(github\.ref, '([^']+)'\) \}\}", expression)
    assert m, expression
    assert "refs/tags/v1.0.0".startswith(m.group(1)), "a release tag must be blocking"
    assert not "refs/heads/main".startswith(m.group(1))
    tags = workflow[True]["push"]["tags"]  # PyYAML reads the key `on` as True
    assert any(re.fullmatch(t.replace("*", ".*"), "v1.0.0") for t in tags)
