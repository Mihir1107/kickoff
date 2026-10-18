"""The production-readiness gate (`scripts/production_readiness.py`): a break the frontend has not
absorbed (an allow-list entry with `status: pending_frontend`) refuses production. It fails the CI
job `production-readiness` on release tags (elsewhere the job warns: annotations and a summary,
green) and the `report` (production) target of the report image, whose `readiness` stage runs it."""

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


def _mode_for(expression: str, ref: str) -> str:
    """The subset of GitHub's expression language the job's step uses (startsWith, &&, ||)."""

    def one(match: re.Match[str]) -> str:
        body = match.group(1).strip().replace("github.ref", repr(ref))
        body = re.sub(r"startsWith\(([^,]+), ([^)]+)\)", r"\1.startswith(\2)", body)
        body = body.replace("&&", " and ").replace("||", " or ")
        return str(eval(body, {"__builtins__": {}}))  # noqa: S307 (our workflow, a fixed subset)

    return re.sub(r"\$\{\{(.*?)\}\}", one, expression)


def _ci_step() -> tuple[dict[str, object], str]:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    job = workflow["jobs"]["production-readiness"]
    runs = [s["run"] for s in job["steps"] if str(s.get("run", "")).startswith("make production-")]
    assert len(runs) == 1, runs
    return job, runs[0]


def _make_command(run: str, ref: str) -> list[str]:
    """What `make` would run for the CI step on this ref (dry run, so the Makefile is in the test)."""
    m = re.fullmatch(r"make production-readiness READINESS_MODE=(\$\{\{.*\}\})", run)
    assert m, run
    mode = _mode_for(m.group(1), ref)
    done = subprocess.run(
        ["make", "-n", "production-readiness", f"READINESS_MODE={mode}"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )  # fmt: skip
    (line,) = [x for x in done.stdout.splitlines() if "production_readiness.py" in x]
    return line.split()


@pytest.mark.parametrize(
    ("ref", "blocking"),
    [("refs/tags/v1.0.0", True), ("refs/heads/main", False), ("refs/heads/feat/x", False),
     ("refs/pull/7/merge", False), ("refs/tags/nightly", False)],
)  # fmt: skip
def test_ci_warns_on_main_and_fails_only_release_tags(
    tmp_path: Path, ref: str, blocking: bool
) -> None:
    """The job as configured, per ref: the mode the workflow picks, through the Makefile, run on a
    list with a pending entry with GitHub's environment. A release tag fails; everywhere else the
    job succeeds with one warning annotation per item and a summary naming them."""
    job, run = _ci_step()
    assert "continue-on-error" not in job, "red must mean something: the job's verdict is its own"
    command = _make_command(run, ref)
    assert command[:3] == ["uv", "run", "python"]
    target = tmp_path / "summary.md"
    done = subprocess.run(
        [sys.executable, *command[3:], "--allowlist", str(_list(tmp_path, [ABSORBED, PENDING]))],
        capture_output=True, text=True, check=False, cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(target)},
    )  # fmt: skip
    assert done.returncode == (1 if blocking else 0), done.stdout + done.stderr
    level = "error" if blocking else "warning"
    annotations = [x for x in done.stdout.splitlines() if x.startswith("::")]
    assert annotations == [
        f"::{level} title=Not ready for production::pending_frontend: {PENDING['rule']} "
        f"get_thing (tracking {PENDING['tracking']})"
    ]
    text = target.read_text()
    assert "1 item(s) refuse a production build" in text and PENDING["tracking"] in text
    assert ("Release blocked" in text) is blocking


def test_nothing_pending_is_green_and_says_so(tmp_path: Path) -> None:
    target = tmp_path / "summary.md"
    for mode in ("enforce", "warn"):
        done = subprocess.run(
            [sys.executable, "scripts/production_readiness.py", "--mode", mode,
             "--allowlist", str(_list(tmp_path, [ABSORBED]))],
            capture_output=True, text=True, check=False, cwd=ROOT,
            env={"PATH": "/usr/bin:/bin", "GITHUB_ACTIONS": "true",
                 "GITHUB_STEP_SUMMARY": str(target)},
        )  # fmt: skip
        assert done.returncode == 0 and "::" not in done.stdout, done.stdout
    assert target.read_text().count("Nothing refuses a production build") == 2


def test_warn_mode_still_fails_a_malformed_list(tmp_path: Path) -> None:
    """Warning mode reports pending items; it never turns a broken allow-list into green."""
    with pytest.raises(api_compat.CompatError):
        production_readiness.main(
            ["--mode", "warn", "--allowlist", str(_list(tmp_path, [{"rule": "x"}]))]
        )


def test_make_and_the_image_enforce_by_default() -> None:
    done = subprocess.run(
        ["make", "-n", "production-readiness"], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert "--mode enforce" in done.stdout
    _, body = _stages((ROOT / "deploy" / "report-image" / "Dockerfile").read_text())["readiness"]
    assert "production_readiness.py --mode enforce" in body


def test_release_tags_trigger_the_workflow() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    tags = workflow[True]["push"]["tags"]  # PyYAML reads the key `on` as True
    assert any(re.fullmatch(t.replace("*", ".*"), "v1.0.0") for t in tags)
