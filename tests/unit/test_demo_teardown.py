"""`scripts/demo.sh` always starts from its own clean stack: it removes every container, volume and
network of compose project ``edisc-demo``, including a stack another checkout left, even when this
checkout has no ``.env.demo`` (a fresh clone), and refuses to go on when any of it remains.

The script runs for real against a fake ``docker`` on PATH that keeps its containers, volumes and
networks in a JSON file and refuses every command it does not model (so a ``compose up`` after a
failed teardown would show)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parents[2] / "scripts" / "demo.sh"
LABEL = "label=com.docker.compose.project="

FAKE_DOCKER = r"""
import json, os, sys
state_path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")
LABEL = "label=com.docker.compose.project="

def project():
    (flt,) = [a for a in args if a.startswith(LABEL)]
    return flt[len(LABEL):]

def remove(kind, names):
    stuck = [n for n in names if n in state["stuck"]]
    state[kind] = [o for o in state[kind] if o["id"] not in names or o["id"] in stuck]
    json.dump(state, open(state_path, "w"))
    for n in stuck:
        sys.stderr.write(f"Error response from daemon: {n} is in use\n")
    sys.exit(1 if stuck else 0)

def listing(kind):
    print("\n".join(o["id"] for o in state[kind] if o["project"] == project()))
    sys.exit(0)

if args[:2] == ["ps", "-aq"]:
    listing("containers")
if args[:3] in (["volume", "ls", "-q"], ["network", "ls", "-q"]):
    listing(args[0] + "s")
if args[0] == "inspect":
    ids = [a for a in args[3:]]
    for o in state["containers"]:
        if o["id"] in ids:
            print(o["working_dir"])
    sys.exit(0)
if args[:2] == ["rm", "-f"]:
    remove("containers", [a for a in args[2:] if a != "-v"])
if args[:3] == ["volume", "rm", "-f"]:
    remove("volumes", args[3:])
if args[:2] == ["network", "rm"]:
    remove("networks", args[2:])
sys.stderr.write("fake docker: not modelled: " + " ".join(args) + "\n")
sys.exit(99)
"""


def _checkout(tmp: Path) -> Path:
    """A checkout without .env.demo, as a fresh clone has, holding only what the teardown needs."""
    root = tmp / "fresh-clone"
    (root / "scripts").mkdir(parents=True)
    (root / "infra").mkdir()
    shutil.copy(DEMO, root / "scripts" / "demo.sh")
    (root / "demo-output").mkdir()
    (root / "demo-output" / "summary.json").write_text("{}")
    return root


def _fake_docker(tmp: Path, state: dict[str, object]) -> dict[str, str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f"#!{sys.executable}\n{FAKE_DOCKER}")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    (tmp / "state.json").write_text(json.dumps(state))
    (tmp / "docker.log").write_text("")
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(tmp / "state.json"),
        "FAKE_DOCKER_LOG": str(tmp / "docker.log"),
        "MIN_FREE_GB": "0",
    }


def _leftover(working_dir: str, stuck: list[str] | None = None) -> dict[str, object]:
    return {
        "containers": [
            {"id": "c-postgres", "project": "edisc-demo", "working_dir": working_dir},
            {"id": "c-minio", "project": "edisc-demo", "working_dir": working_dir},
            {"id": "c-test-stack", "project": "edisc-test", "working_dir": "/somewhere/infra"},
        ],
        "volumes": [
            {"id": "edisc-demo_pgdata", "project": "edisc-demo"},
            {"id": "edisc-demo_miniodata", "project": "edisc-demo"},
            {"id": "edisc-test_pgdata", "project": "edisc-test"},
        ],
        "networks": [{"id": "n-demo", "project": "edisc-demo"}],
        "stuck": stuck or [],
    }


def _run(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "demo.sh"), *args],
        env=env, capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip


def _left(tmp: Path) -> dict[str, list[str]]:
    state = json.loads((tmp / "state.json").read_text())
    return {k: sorted(o["id"] for o in state[k]) for k in ("containers", "volumes", "networks")}


def test_a_stack_another_checkout_left_is_removed_without_an_env_file(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    env = _fake_docker(tmp_path, _leftover("/Users/someone/other-checkout/infra"))
    assert not (root / ".env.demo").exists()
    done = _run(root, env, "down")
    assert done.returncode == 0, done.stderr
    # every container, volume and network of edisc-demo gone; another project's untouched
    assert _left(tmp_path) == {
        "containers": ["c-test-stack"], "volumes": ["edisc-test_pgdata"], "networks": [],
    }  # fmt: skip
    assert "another checkout left: /Users/someone/other-checkout/infra" in done.stdout
    assert not (root / "demo-output").exists()


def test_this_checkouts_own_stack_is_removed_without_naming_another(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    env = _fake_docker(tmp_path, _leftover(f"{root.resolve()}/infra"))
    done = _run(root, env, "down")
    assert done.returncode == 0, done.stderr
    assert _left(tmp_path)["volumes"] == ["edisc-test_pgdata"]
    assert "another checkout" not in done.stdout


def test_a_stack_that_cannot_be_removed_refuses_the_demo_before_anything_starts(
    tmp_path: Path,
) -> None:
    root = _checkout(tmp_path)
    env = _fake_docker(tmp_path, _leftover("/elsewhere/infra", stuck=["edisc-demo_pgdata"]))
    done = _run(root, env)  # a full demo run: step 0 must stop it
    assert done.returncode == 1
    assert "refusing: the edisc-demo stack" in done.stderr
    assert "label=com.docker.compose.project=edisc-demo" in done.stderr
    assert _left(tmp_path)["volumes"] == ["edisc-demo_pgdata", "edisc-test_pgdata"]
    commands = {line.split()[0] for line in (tmp_path / "docker.log").read_text().splitlines()}
    assert commands <= {"rm", "ps", "inspect", "volume", "network"}  # no compose, buildx or run
    assert (root / "demo-output" / "summary.json").exists()  # nothing of the old run deleted either


def test_down_refuses_too_and_keeps_the_output(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    env = _fake_docker(tmp_path, _leftover("/elsewhere/infra", stuck=["c-postgres"]))
    done = _run(root, env, "down")
    assert done.returncode == 1
    assert "refusing" in done.stderr
    assert (root / "demo-output" / "summary.json").exists()
    assert LABEL + "edisc-demo" in done.stderr
