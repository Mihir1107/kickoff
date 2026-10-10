"""The report PDF on a REAL report worker in the report image (ADR 0018 §6, §13, amendments 3 and 7):
the OOM crash point, by both kill paths, and heartbeats while the PDF child works.

The worker runs as a container of the image (`EDISC_REPORT_IMAGE`) against the test stack; the test
process drives the workflow and inspects records. Skipped, with a visible reason, without the image
(`make test-report-pdf` builds it and runs this; the CI `report-pdf` job runs it natively on amd64).

OOM: a worker whose PDF child cannot fit one render starts the report; the CHILD dies (its own
`RLIMIT_AS`, or the container's cgroup limit, where `oom_score_adj` = 1000 makes the kernel pick the
child); the worker process stays alive and heartbeating; the attempt fails with the classified
retryable `ReportPdfRenderError`; no PDF object version, no `report_files` row, no
`report_generated`; the report stays `generating`. Then the limit is raised and the RETRY completes
the report to the oracle's bytes (the image's PDF of the stored `report.html`). Limits come from
measurements in the image (the child's peak after importing WeasyPrint and rendering a tiny page),
so a render of the capped report needs more but the child can start: it dies while laying out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import unicodedata
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from temporalio.client import Client, WorkflowHandle
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_renderers.report.version import REPORT_RENDERER_VERSION
from edisc_worker.contracts import ReportRef, report_task_queue, report_workflow_id
from edisc_worker.workflows import ReportWorkflow

from ..normalizer.harness import Sessions, Tenant, new_tenant
from ..report.synthetic import sealed_job, unit_keys
from .conftest import new_report, report_state, stored_bytes

IMAGE = os.environ.get("EDISC_REPORT_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="needs the pinned linux/amd64 report image: run make test-report-pdf"
)
ROOT = Path(__file__).resolve().parents[3]
FAST = {"heartbeat_timeout_seconds": 3, "retry_initial_seconds": 0.5, "retry_max_seconds": 2.0}
MiB = 2**20


def _docker(*args: str, stdin: bytes | None = None, check: bool = True) -> bytes:
    out = subprocess.run(["docker", *args], input=stdin, capture_output=True, check=False)
    if check and out.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed: {out.stderr.decode()[-2000:]}")
    return out.stdout


async def adocker(*args: str, stdin: bytes | None = None, check: bool = True) -> bytes:
    """`_docker` off the event loop (docker commands take seconds)."""
    return await asyncio.to_thread(_docker, *args, stdin=stdin, check=check)


def _python(*args: str, stdin: bytes | None = None) -> bytes:
    assert IMAGE
    return _docker("run", "--rm", "-i", "--platform", "linux/amd64", "--entrypoint",
                   "/opt/edisc/venv/bin/python", IMAGE, *args, stdin=stdin)  # fmt: skip


def _container_env() -> list[str]:
    """The test stack's settings for a worker container: host networking on Linux; elsewhere
    (Docker Desktop) the host is host.docker.internal."""
    values: dict[str, str] = {}
    for raw in (ROOT / os.environ.get("EDISC_ENV_FILE", ".env.test")).read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            v = v.strip()
            values[k.strip()] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] == "'" else v
    if sys.platform != "linux":
        values = {k: v.replace("localhost", "host.docker.internal").replace(
                  "127.0.0.1", "host.docker.internal") for k, v in values.items()}  # fmt: skip
    return [f"{k}={v}" for k, v in values.items() if k.startswith(("EDISC_", "TEMPORAL_"))]


def _network() -> list[str]:
    return ["--network", "host"] if sys.platform == "linux" else []


async def _worker(name: str, queue: str, *, memory: str, env: dict[str, str]) -> AsyncIterator[str]:
    args = ["run", "-d", "--name", name, "--platform", "linux/amd64", *_network(),
            "--memory", memory, "--memory-swap", memory, "--cpus", "1",
            "--entrypoint", "/opt/edisc/venv/bin/python"]  # fmt: skip
    for kv in _container_env() + [f"{k}={v}" for k, v in env.items()]:
        args += ["-e", kv]
    assert IMAGE
    await adocker(*args, IMAGE, "-m", "edisc_worker", "--reports", "--reports-queue", queue, "--source", "dummy",
            "--queue", f"collect-unused-{uuid.uuid4().hex[:8]}")  # fmt: skip
    try:
        yield name
    finally:
        logs = await asyncio.to_thread(
            subprocess.run,
            ["docker", "logs", "--tail", "60", name],
            capture_output=True,
            check=False,
        )
        logs_text = (logs.stdout + logs.stderr).decode(errors="replace")
        await adocker("rm", "-f", name, check=False)
        print(f"--- worker {name} log tail ---\n{logs_text}")


async def _inspect(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(await adocker("inspect", name))[0]
    return doc


@pytest.fixture(scope="module")
def image_facts() -> dict[str, Any]:
    tid = json.loads(_python("-m", "edisc_worker.versions"))["toolchain_id"]
    probe = ("from edisc_worker.report_pdf import render_pdf_bytes\n"
             "render_pdf_bytes(b'<!doctype html><title>t</title><p>x</p>', 'letter')\n"
             "s = dict(l.split(':', 1) for l in open('/proc/self/status'))\n"
             "print(int(s['VmPeak'].split()[0]) * 1024, int(s['VmHWM'].split()[0]) * 1024)")  # fmt: skip
    peak, hwm = (int(x) for x in _python("-c", probe).split())
    return {"toolchain": tid, "small_vm_peak": peak, "small_rss": hwm}


async def _big_report(
    sessions: Sessions, s3: S3Client, settings: Settings, toolchain: str
) -> tuple[Tenant, uuid.UUID, uuid.UUID]:
    """A report at the caps (1,001 conversations, every other one with a gap): the largest HTML."""
    t = await new_tenant(sessions)
    job = await sealed_job(sessions, s3, settings, t, unit_keys(1_001, 2), gap_every=2)
    identity = {"renderer_version": REPORT_RENDERER_VERSION, "toolchain_id": toolchain,
                "unicode_version": unicodedata.unidata_version}  # fmt: skip
    return t, job, await new_report(sessions, t, job, identity=identity)


async def _pending(handle: WorkflowHandle[Any, Any]) -> list[Any]:
    return list((await handle.describe()).raw_description.pending_activities)


async def _pdf_failure(handle: WorkflowHandle[Any, Any], worker: str) -> Any:
    """The `report_files` activity once it has FAILED an attempt (attempt >= 2)."""
    async with asyncio.timeout(600):
        while True:
            assert (await _inspect(worker))["State"]["Running"], f"worker {worker} exited"
            for act in await _pending(handle):
                if act.activity_type.name == "report_files" and act.attempt >= 2:
                    return act
            await asyncio.sleep(1)


async def _no_pdf_written(sessions: Sessions, t: Tenant, report_id: uuid.UUID) -> None:
    st = await report_state(sessions, t.tenant_id, report_id)
    assert st["row"].status == "generating"
    assert "report.pdf" not in [f.name for f in st["files"]]
    assert "report_generated" not in st["types"]
    async with tenant_tx(sessions, t.tenant_id) as s:
        pdfs = (await s.execute(text("SELECT count(*) FROM evidence_objects WHERE report_id = :r"
                                     " AND storage_key LIKE '%/report.pdf'"),
                                {"r": report_id})).scalar_one()  # fmt: skip
    assert pdfs == 0


async def _completed_to_oracle(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, report_id: uuid.UUID,
    handle: WorkflowHandle[Any, Any],
) -> None:  # fmt: skip
    async with asyncio.timeout(900):
        result = await handle.result()
    assert result["status"] == "completed", result
    st = await report_state(sessions, t.tenant_id, report_id)
    assert [f.name for f in st["files"]][-1] == "report.pdf"
    stored = await stored_bytes(s3, settings, t, report_id, st["files"])
    oracle = await asyncio.to_thread(_python, "-m", "edisc_worker.report_pdf", "--paper", "letter", "--memory-bytes",
                     str(5 * 2**29), stdin=stored["report.html"])  # fmt: skip
    assert hashlib.sha256(stored["report.pdf"]).digest() == hashlib.sha256(oracle).digest()


@pytest.mark.timeout(1800)
@pytest.mark.parametrize("path", ["rlimit", "cgroup"])
async def test_an_oom_of_the_pdf_child_fails_the_attempt_and_the_retry_completes(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client,
    image_facts: dict[str, Any], path: str,
) -> None:  # fmt: skip
    t, _, report_id = await _big_report(app_sessions, s3, settings, image_facts["toolchain"])
    queue = f"{report_task_queue(REPORT_RENDERER_VERSION, image_facts['toolchain'], unicodedata.unidata_version)}.t-{uuid.uuid4().hex[:6]}"
    name = f"edisc-report-oom-{path}-{uuid.uuid4().hex[:6]}"
    if path == "rlimit":  # the child's own limit: room to start and render a tiny page, no more
        env = {"EDISC_REPORT_PDF_MEMORY_BYTES": str(image_facts["small_vm_peak"] + 64 * MiB)}
        memory = "3g"
    else:  # the container's cgroup limit, the child's own limit far above it
        env = {"EDISC_REPORT_PDF_MEMORY_BYTES": str(5 * 2**29)}
        memory = "3g"
    gen = _worker(name, queue, memory=memory, env=env)
    await gen.__anext__()
    try:
        if (
            path == "cgroup"
        ):  # idle worker's usage + a tiny render's peak: the big render cannot fit
            await asyncio.sleep(5)
            base = int(await adocker("exec", name, "cat", "/sys/fs/cgroup/memory.current"))
            limit = str(base + image_facts["small_rss"] + 48 * MiB)
            await adocker("update", "--memory", limit, "--memory-swap", limit, name)
        started = (await _inspect(name))["State"]["StartedAt"]
        handle = await temporal.start_workflow(
            ReportWorkflow.run, ReportRef(str(t.tenant_id), str(report_id), max_attempts=25, **FAST),
            id=report_workflow_id(str(report_id)), task_queue=queue,
        )  # fmt: skip
        act = await _pdf_failure(handle, name)
        failure = act.last_failure
        message = f"{failure.message} {failure.cause.message if failure.HasField('cause') else ''}"
        assert "PDF child" in message, message  # classified, not a heartbeat timeout or crash
        assert failure.application_failure_info.type != "" and "Timeout" not in failure.message
        if path == "cgroup":
            assert "SIGKILL" in message, message  # the kernel killed the CHILD
        info = await _inspect(name)  # the worker process survived: same start, never restarted
        assert info["State"]["Running"] and info["State"]["StartedAt"] == started
        assert info["RestartCount"] == 0
        await _no_pdf_written(app_sessions, t, report_id)
        async with asyncio.timeout(60):  # the worker is still heartbeating its retry attempt
            while not any(a.HasField("last_heartbeat_time") for a in await _pending(handle)):  # noqa: ASYNC110 - remote state
                await asyncio.sleep(0.5)

        if path == "rlimit":  # a worker with the normal limit takes the retry
            await gen.aclose()
            gen = _worker(name + "-b", queue, memory="3g", env={})
            await gen.__anext__()
        else:  # the same worker process, the container limit raised
            await adocker("update", "--memory", "3g", "--memory-swap", "3g", name)
        await _completed_to_oracle(app_sessions, s3, settings, t, report_id, handle)
    finally:
        await gen.aclose()


@pytest.mark.timeout(900)
async def test_heartbeats_flow_while_the_pdf_child_is_held(
    app_sessions: Sessions, s3: S3Client, settings: Settings, temporal: Client,
    image_facts: dict[str, Any],
) -> None:  # fmt: skip
    """§6: the child held for more than twice the heartbeat timeout; ONE attempt, no timeout."""
    t = await new_tenant(app_sessions)
    job = await sealed_job(app_sessions, s3, settings, t, unit_keys(5))
    identity = {"renderer_version": REPORT_RENDERER_VERSION, "toolchain_id": image_facts["toolchain"],
                "unicode_version": unicodedata.unidata_version}  # fmt: skip
    report_id = await new_report(app_sessions, t, job, identity=identity)
    queue = f"{report_task_queue(**identity)}.t-{uuid.uuid4().hex[:6]}"
    hold = str(2.5 * FAST["heartbeat_timeout_seconds"])
    gen = _worker(f"edisc-report-hold-{uuid.uuid4().hex[:6]}", queue, memory="3g",
                  env={"EDISC_TEST_REPORT_PDF_HOLD_SECONDS": hold})  # fmt: skip
    await gen.__anext__()
    try:
        handle = await temporal.start_workflow(
            ReportWorkflow.run, ReportRef(str(t.tenant_id), str(report_id), max_attempts=1, **FAST),
            id=report_workflow_id(str(report_id)), task_queue=queue,
        )  # fmt: skip
        async with asyncio.timeout(600):
            result = await handle.result()
        assert result["status"] == "completed", result
        st = await report_state(app_sessions, t.tenant_id, report_id)
        assert [f.name for f in st["files"]][-1] == "report.pdf"
    finally:
        await gen.aclose()
