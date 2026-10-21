"""The standalone verifier answers exactly like the in-tree one (M18 step 5, plan §5).

`make verifier-standalone` builds the `edisc-verify` wheel twice (reproducible), installs it into an
EMPTY venv from the wheel and the hashed requirements (`--no-index` after one download), and runs
this test with that venv's executable in `EDISC_VERIFY_STANDALONE`. On real packages made through
the API (a sealed job's custody package, a render package, a report package with and without the
job package recomputation, zipped and unzipped) and on tampered copies, the standalone executable
must give the same exit status and the same output, byte for byte, as `python -m edisc_verify.cli`
from the workspace. Its own CI job and marker (`verifier_standalone`); excluded from the shards.
Run outside that job it FAILS (never skips): the job is where it belongs."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.worker import Worker

from edisc_connector_dummy.connector import DummyConnector
from edisc_worker.activities import Activities
from edisc_worker.contracts import task_queue
from edisc_worker.renders import RenderActivities
from edisc_worker.reports import ReportActivities
from edisc_worker.workflows import RenderWorkflow, ReportWorkflow

from ..worker.conftest import WORKFLOWS
from .conftest import Api, TenantCtx
from .test_renders import wait_render
from .test_reports import _edit_jsonl, _reported

pytestmark = [pytest.mark.verifier_standalone, pytest.mark.timeout(600)]
ENV = "EDISC_VERIFY_STANDALONE"


@pytest.fixture
async def workers(api: Api) -> AsyncIterator[None]:
    """ONE collection worker (Temporal refuses two overlapping workers on `collect-dummy`), plus
    the render and report workers of this runtime, on the queues the API uses."""
    collect = Activities(
        api.sessions, api.s3, api.settings.model_copy(update={"activity_time_box_seconds": 30}),
        {"dummy": DummyConnector(api.resources.limiter)}, api.temporal,
    )  # fmt: skip
    rendering = RenderActivities(api.sessions, api.s3, api.settings)
    reporting = ReportActivities(api.sessions, api.s3, api.settings)
    async with (
        Worker(api.temporal, task_queue=task_queue("dummy"), workflows=WORKFLOWS,
               activities=collect.all()),
        Worker(api.temporal, task_queue=rendering.task_queue, workflows=[RenderWorkflow],
               activities=rendering.all()),
        Worker(api.temporal, task_queue=reporting.task_queue, workflows=[ReportWorkflow],
               activities=reporting.all()),
    ):  # fmt: skip
        yield


def _run(argv: list[str], args: list[Path | str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        [*argv, *map(str, args)], capture_output=True, text=True, timeout=300, check=False,
        env={"PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    return proc.returncode, proc.stdout, proc.stderr


def _unzip(data: bytes, dest: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)
    return dest


def _flip_byte(path: Path, at: int = -2) -> None:
    data = bytearray(path.read_bytes())
    data[at] ^= 0x01
    path.write_bytes(bytes(data))


def _standalone() -> str:
    standalone = os.environ.get(ENV)
    if not standalone or not Path(standalone).is_file():
        pytest.fail(f"{ENV} must name the standalone venv's edisc-verify: make verifier-standalone")
    assert Path(standalone).resolve().parent != Path(sys.executable).resolve().parent
    return standalone


Case = tuple[str, list[Path | str], bool]


def _cases(tmp_path: Path, packages: dict[str, bytes]) -> list[Case]:
    """The packages, zipped and unzipped, and tampered copies: the chain, an unlisted file, a
    rendered output, the report, a truncated zip."""
    for name, content in packages.items():
        (tmp_path / f"{name}.zip").write_bytes(content)
    d = {name: _unzip(content, tmp_path / name) for name, content in packages.items()}
    bad_chain = Path(shutil.copytree(d["job"], tmp_path / "bad-chain"))
    _edit_jsonl(bad_chain / "events.jsonl", "unit_reconciled", "collected", 999)
    unlisted = Path(shutil.copytree(d["job"], tmp_path / "unlisted"))
    (unlisted / "extra.txt").write_text("not in the manifest\n")
    bad_render = Path(shutil.copytree(d["render"], tmp_path / "bad-render"))
    outputs = [p for p in (bad_render / "outputs").rglob("*") if p.is_file()]
    _flip_byte(max(outputs, key=lambda p: p.stat().st_size))
    bad_report = Path(shutil.copytree(d["report"], tmp_path / "bad-report"))
    _flip_byte(bad_report / "outputs" / "report.json", at=len(b'{"'))
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(packages["job"][:-100])
    job, report = tmp_path / "job.zip", tmp_path / "report.zip"
    return [
        ("job package (zip)", [job], True),
        ("job package (directory)", [d["job"]], True),
        ("render package (zip)", [tmp_path / "render.zip"], True),
        ("render package (directory)", [d["render"]], True),
        ("report package", [report], True),
        ("report package, recomputed", [report, "--job-package", job], True),
        ("tampered chain", [bad_chain], False),
        ("unlisted file", [unlisted], False),
        ("tampered render output", [bad_render], False),
        ("tampered report", [bad_report, "--job-package", d["job"]], False),
        ("report against a tampered job package", [report, "--job-package", bad_chain], False),
        ("truncated zip", [truncated], False),
    ]


async def test_the_standalone_verifier_answers_exactly_like_the_in_tree_one(
    api: Api, tenant: TenantCtx, workers: None, tmp_path: Path
) -> None:
    standalone = await asyncio.to_thread(_standalone)
    _, job_id, report = await _reported(api, tenant)
    async with api.client(tenant.subdomain, tenant.token(api.settings)) as c:
        made = await c.post(f"/v1/jobs/{job_id}/renders", json={})
        assert made.status_code in (200, 201, 202), made.text
        render = await wait_render(c, made.json()["id"])
        assert (render["status"], render["sealed"]) == ("completed", True), render
        got = {
            "job": await c.get(f"/v1/jobs/{job_id}/package"),
            # outputs embedded (the default references them by hash): tampering needs the bytes
            "render": await c.get(
                f"/v1/renders/{render['id']}/package", params={"outputs": "embed"}
            ),
            "report": await c.get(f"/v1/reports/{report['id']}/package"),
        }
    for name, r in got.items():
        assert r.status_code == 200, (name, r.text[:300])
    cases = await asyncio.to_thread(_cases, tmp_path, {n: r.content for n, r in got.items()})
    in_tree = [sys.executable, "-m", "edisc_verify.cli"]
    for label, args, ok in cases:
        mine, theirs = await asyncio.gather(
            asyncio.to_thread(_run, in_tree, args), asyncio.to_thread(_run, [standalone], args)
        )
        assert theirs == mine, (label, mine, theirs)
        assert (mine[0] == 0) is ok, (label, mine)
