"""A production install starts (ADR 0018 §26): every runtime dependency is declared. The API and the
worker are installed the way an image installs them -- `uv sync --frozen --no-dev --package <app>
--no-editable` into a fresh venv, so nothing from the dev group (pytest, hypothesis, html5lib,
pypdf, ...) and no workspace source is importable -- then the API and EVERY worker type are started
from those venvs against the test stack and must come up: the API answers HTTP, each worker logs
`worker started` with its task queues and is still running. A module importing something only the
dev group provides fails here, not in production (`types_aiobotocore_s3` once did, ADR 0018 §22.2).

Its own CI job (`production-install`) and marker; excluded from the integration shards."""

from __future__ import annotations

import asyncio
import os
import socket
import tomllib
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT / ".env.test"
# one process per worker type: each production source, and each optional role
WORKERS: dict[str, list[str]] = {
    "source slack_export": ["--source", "slack_export"],
    "source dummy (test stacks only)": ["--source", "dummy"],
    "maintenance": ["--source", "slack_export", "--maintenance"],
    "exports": ["--source", "slack_export", "--exports"],
    "renders": ["--source", "slack_export", "--renders"],
    "reports": ["--source", "slack_export", "--reports"],
}
pytestmark = [pytest.mark.production_install, pytest.mark.timeout(900)]


async def _run(*argv: str, env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    async with asyncio.timeout(600):
        out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    assert proc.returncode == 0, f"{argv}: exit {proc.returncode}\n{text[-4000:]}"
    return text


def _members() -> list[str]:
    """Every workspace member: their wheels are rebuilt from the tree on each sync (uv reuses a
    cached build of a local package until its pyproject changes, which would test stale code)."""
    names = []
    for pyproject in sorted(ROOT.glob("*/*/pyproject.toml")) + sorted(
        ROOT.glob("*/*/*/pyproject.toml")
    ):
        project = tomllib.loads(pyproject.read_text()).get("project", {})
        if str(project.get("name", "")).startswith("edisc-"):
            names.append(project["name"])
    return names


async def _venv(tmp: Path, package: str) -> Path:
    venv = tmp / package
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv), "UV_LINK_MODE": "copy"}
    env.pop("VIRTUAL_ENV", None)
    fresh = [arg for name in _members() for arg in ("--refresh-package", name)]
    await _run("uv", "sync", "--frozen", "--no-dev", "--package", package, "--no-editable",
               *fresh, env=env)  # fmt: skip
    python = venv / "bin" / "python"
    # a production install: the dev group is absent, and no module comes from the source tree
    probe = await asyncio.create_subprocess_exec(
        str(python), "-c", "import pytest", cwd="/", stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    assert await probe.wait() != 0, f"{package}: pytest is importable from the production venv"
    where = await _run(str(python), "-c", "import edisc_core; print(edisc_core.__file__)",
                       env={"PATH": os.environ["PATH"]})  # fmt: skip
    assert str(venv) in where, f"{package}: edisc_core imported from {where.strip()}"
    return python


def _app_env() -> dict[str, str]:
    """What a deployment gives the processes: the stack's settings (never the dev group, never the
    source tree on sys.path)."""
    keep = ("PATH", "HOME", "TMPDIR", "LANG")
    return {
        **{k: os.environ[k] for k in keep if k in os.environ},
        "EDISC_ENV_FILE": str(ENV_FILE),
        "PYTHONSAFEPATH": "1",
    }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 30)
        except TimeoutError:
            proc.kill()
            await proc.wait()


async def _api_comes_up(python: Path, tmp: Path) -> None:
    port = _free_port()
    log = (tmp / "api.log").open("wb")
    proc = await asyncio.create_subprocess_exec(
        str(python), "-m", "uvicorn", "edisc_api.main:app", "--host", "127.0.0.1", "--port",
        str(port), cwd=tmp, env=_app_env(), stdout=log, stderr=log,
    )  # fmt: skip
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            for _ in range(240):
                if proc.returncode is not None:
                    break
                try:
                    resp = await client.get(f"http://127.0.0.1:{port}/v1/me")
                except httpx.TransportError:
                    await asyncio.sleep(0.5)
                    continue
                assert resp.status_code == 401, resp.text  # up, and refusing the anonymous call
                return
        pytest.fail(f"the API did not come up:\n{(tmp / 'api.log').read_text()[-4000:]}")
    finally:
        await _stop(proc)
        log.close()


async def _worker_comes_up(python: Path, tmp: Path, name: str, args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        str(python), "-m", "edisc_worker", *args, cwd=tmp, env=_app_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    assert proc.stdout is not None
    seen: list[str] = []
    try:
        async with asyncio.timeout(180):
            while True:
                line = (await proc.stdout.readline()).decode(errors="replace")
                if not line:
                    pytest.fail(
                        f"worker {name} exited {await proc.wait()}:\n{''.join(seen)[-4000:]}"
                    )
                seen.append(line)
                if "worker started" in line:
                    break
        await asyncio.sleep(3)  # it keeps running (polling), not a start-then-crash
        assert proc.returncode is None, f"worker {name} stopped:\n{''.join(seen)[-4000:]}"
    finally:
        await _stop(proc)


async def test_the_api_and_every_worker_type_start_from_a_production_install(
    tmp_path: Path,
) -> None:
    assert ENV_FILE.is_file(), "needs the test stack (make test-env-up)"
    api, worker = await asyncio.gather(
        _venv(tmp_path, "edisc-api"), _venv(tmp_path, "edisc-worker")
    )
    await _api_comes_up(api, tmp_path)
    for name, args in WORKERS.items():
        await _worker_comes_up(worker, tmp_path, name, args)
