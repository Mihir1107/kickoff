"""CI gives every main commit its own full result (the fixes before M17 step 6).

Before: one concurrency group per ref with ``cancel-in-progress: true``, so a push to main
cancelled the previous main commit's integration shards (run 38887605999). Now branches and pull
requests still cancel older runs, while on main each commit has its own group (GitHub cancels a
PENDING run of a group even without ``cancel-in-progress``) and nothing is cancelled. The two
GitHub expressions are evaluated here for both kinds of ref."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"


def _evaluate(expression: str, ref: str, sha: str) -> str:
    """The subset of GitHub's expression language the concurrency block uses."""

    def one(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        body = body.replace("github.ref", repr(ref)).replace("github.sha", repr(sha))
        body = re.sub(r"format\('-\{0\}', ('[^']*')\)", r"('-' + \1)", body)
        body = body.replace("&&", " and ").replace("||", " or ")
        value = eval(body, {"__builtins__": {}})  # noqa: S307 (our own workflow file, a fixed subset)
        return str(value).lower() if isinstance(value, bool) else str(value)

    return re.sub(r"\$\{\{(.*?)\}\}", one, expression)


def test_main_commits_are_never_cancelled_and_branches_are() -> None:
    block = yaml.safe_load(WORKFLOW.read_text())["concurrency"]
    group, cancel = block["group"], str(block["cancel-in-progress"])
    main = [(_evaluate(group, "refs/heads/main", sha), _evaluate(cancel, "refs/heads/main", sha))
            for sha in ("aaa1", "bbb2")]  # fmt: skip
    assert main[0][0] != main[1][0], "two main commits share a group: the older may be cancelled"
    assert {c for _, c in main} == {"false"}
    branch = [_evaluate(group, "refs/heads/feat/x", sha) for sha in ("aaa1", "bbb2")]
    assert branch[0] == branch[1], "a branch's runs must share a group"
    assert _evaluate(cancel, "refs/pull/7/merge", "aaa1") == "true"
