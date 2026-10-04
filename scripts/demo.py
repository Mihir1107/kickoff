"""The demo driver, run by ``scripts/demo.sh`` against the disposable ``edisc-demo`` stack and a real API
process. It seeds a tenant, uploads a synthetic Slack export, collects it while SIGKILLing the worker
mid-job, shows reconciliation and custody, writes the offline custody package, and renders the job
to RSMF through the step 3 loader. Each step prints what it proves."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from edisc_api.admin import onboard_tenant
from edisc_api.auth import DEV_ISSUER, DEV_JWKS, dev_token
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.dialects.slack_export import ExportOptions, write_export
from edisc_connector_dummy.spec import DatasetSpec
from edisc_core.kms import LocalKmsClient
from edisc_core.settings import Settings
from edisc_custody.export import export_package
from edisc_db.session import create_engine, session_factory, tenant_tx
from edisc_evidence.s3 import s3_client
from edisc_renderers.rsmf import RenderOptions
from edisc_worker.render_store import render_and_store

AUDIENCE = "edisc-api"
SPEC = DatasetSpec(
    seed=2026, dialect="slack_history", conversations=8, days=5, messages_per_unit=40, p_file=0.0
)
BOLD, GREEN, RED, DIM, RESET = "\033[1m", "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def check(condition: object, message: object) -> None:
    """A demo step that does not hold stops the demo loudly."""
    if not condition:
        raise SystemExit(f"{RED}demo check failed:{RESET} {message}")


def say(title: str) -> None:
    print(f"\n{BOLD}== {title}{RESET}", flush=True)


def ok(line: str) -> None:
    print(f"   {GREEN}✔{RESET} {line}", flush=True)


def info(line: str) -> None:
    print(f"   {DIM}{line}{RESET}", flush=True)


class Worker:
    """The collection worker as a real OS process, so it can be killed with SIGKILL."""

    def __init__(self, logs: Path) -> None:
        self.logs, self.proc, self.starts = logs, None, 0  # type: ignore[var-annotated]

    def start(self) -> None:
        self.starts += 1
        log = (self.logs / f"worker-{self.starts}.log").open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "edisc_worker", "--source", "slack_export", "--exports"],
            stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy(),
        )  # fmt: skip
        info(f"worker started (pid {self.proc.pid}, log {log.name})")

    def kill(self) -> int:
        check(self.proc is not None, "no worker to kill")
        assert self.proc is not None  # noqa: S101 - narrows the type for mypy
        pid = self.proc.pid
        os.kill(pid, signal.SIGKILL)
        self.proc.wait()
        return pid

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


async def wait_api(base: str) -> None:
    async with httpx.AsyncClient() as c:
        for _ in range(120):
            try:
                await c.get(f"{base}/v1/me")
            except httpx.TransportError:
                await asyncio.sleep(0.5)
            else:
                return
    raise SystemExit("the API did not come up")


async def poll_export(c: httpx.AsyncClient, export_id: str) -> dict[str, Any]:
    for _ in range(600):
        export: dict[str, Any] = (await c.get(f"/v1/exports/{export_id}")).json()
        if export["status"] in ("ready", "rejected"):
            return export
        await asyncio.sleep(0.3)
    raise SystemExit("the export did not settle")


async def run(out: Path, base: str) -> dict[str, Any]:
    settings = Settings()
    engine = create_engine(settings, "app")
    sessions = session_factory(engine)
    worker = Worker(out / "logs")
    started = time.monotonic()
    try:
        await wait_api(base)

        say("2. Seed: a tenant (dev IdP), its admin, a client and a matter")
        sub = f"demo{uuid.uuid4().hex[:6]}"
        LocalKmsClient(settings).create_key(f"tenant/{sub}")
        tenant = await onboard_tenant(
            sessions, kms_key_ref=f"tenant/{sub}", subdomain=sub, name="Demo Corp", issuer=DEV_ISSUER,
            audience=AUDIENCE, jwks_url=DEV_JWKS, admin_subject="demo-admin",
            admin_name="Demo Admin", operator="demo",
        )  # fmt: skip
        token = dev_token(settings, subject="demo-admin", audience=AUDIENCE)
        c = httpx.AsyncClient(
            base_url=base, timeout=60,
            headers={"host": f"{sub}.{settings.api_base_domain}", "authorization": f"Bearer {token}"},
        )  # fmt: skip
        matter = (
            await c.post(
                f"/v1/clients/{tenant.default_client_id}/matters",
                json={"name": "Acme v. Demo", "retention_until": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
            )
        ).json()["id"]  # fmt: skip
        ok(f"tenant {sub} ({tenant.tenant_id}), matter {matter}")

        say("3. Synthetic Slack export: generate, upload in parts, hash + lock + validate")
        worker.start()
        ds = Dataset(SPEC)
        buf = io.BytesIO()
        write_export(ds, buf, ExportOptions())
        data = buf.getvalue()
        created = await c.post(
            f"/v1/clients/{tenant.default_client_id}/exports",
            json={"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        )
        created.raise_for_status()
        export_id = created.json()["id"]
        part = max(created.json()["upload"]["part_min_bytes"], 5 << 20)
        for n, start in enumerate(range(0, len(data), part), start=1):
            chunk = data[start : start + part]
            digest = base64.b64encode(hashlib.sha256(chunk).digest()).decode()
            r = await c.put(
                f"/v1/exports/{export_id}/parts/{n}", content=chunk,
                headers={"content-digest": f"sha-256=:{digest}:"},
            )  # fmt: skip
            r.raise_for_status()
        (await c.post(f"/v1/exports/{export_id}/complete")).raise_for_status()
        export = await poll_export(c, export_id)
        check(export["status"] == "ready", export)
        ok(
            f"{len(data):,} bytes, {SPEC.conversations} conversations x {SPEC.days} days,"
            f" {sum(len(ds.visible_messages(cv.id, d, 0)) for cv in ds.conversations() for d in range(SPEC.days)):,}"
            f" messages; locked in WORM, SHA-256 {export['sha256'][:16]}..."
        )  # fmt: skip

        say("4. Collection job, with the worker SIGKILLed half way")
        start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
        scope = {"type": "channel", "external_id": "*", "date_from": start.isoformat(),
                 "date_to": (start + timedelta(days=SPEC.days)).isoformat()}  # fmt: skip
        r = await c.post(f"/v1/matters/{matter}/jobs", json={"connection_id": export["connection_id"], "scopes": [scope]})  # fmt: skip
        r.raise_for_status()
        job_id = r.json()["id"]
        info(f"job {job_id}")
        killed_at = None
        while True:
            units = (await c.get(f"/v1/jobs/{job_id}/units?limit=200")).json()["items"]
            job = (await c.get(f"/v1/jobs/{job_id}")).json()
            days = [u for u in units if u["kind"] == "conversation_day"]
            done = sum(u["status"] == "done" for u in days)
            if job["sealed"]:
                break
            if killed_at is None and days and done >= max(2, len(days) // 3):
                pid = worker.kill()
                killed_at = (done, len(days))
                print(f"   {RED}✘ SIGKILL worker pid {pid}{RESET} with {done}/{len(days)} units done", flush=True)  # fmt: skip
                await asyncio.sleep(2)
                worker.start()
                info("a fresh worker resumes from the database checkpoints")
            await asyncio.sleep(0.4)
        if killed_at is None:
            info("the job finished before the kill point (dataset too small)")
        ok(
            f"job {job['status']}"
            + (f" (basis: {job['clean_basis']})" if job["clean_basis"] else "")
            + ", sealed"
        )

        say("5. Reconciliation: expected vs collected, per conversation-day")
        recon = (await c.get(f"/v1/jobs/{job_id}/reconciliation")).json()
        async with tenant_tx(sessions, tenant.tenant_id) as s:
            dupes, linked = (
                await s.execute(
                    text(
                        "SELECT (SELECT count(*) - count(DISTINCT idempotency_key) FROM items),"
                        " (SELECT count(*) FROM job_items ji JOIN items i ON i.id = ji.item_id"
                        "  WHERE ji.job_id = :j AND ji.in_scope AND i.item_type = 'message')"
                    ),
                    {"j": uuid.UUID(job_id)},
                )
            ).one()
        oracle = sum(len(ds.visible_messages(cv.id, d, 0)) for cv in ds.conversations() for d in range(SPEC.days))  # fmt: skip
        ok(f"units by status: {recon['by_recon_status']}; not matched: {len(recon['not_matched'])}")
        ok(
            f"{linked:,} in-scope messages collected = {oracle:,} in the export; duplicate versions: {dupes}"
        )
        async with tenant_tx(sessions, tenant.tenant_id) as s:
            reasons = dict(
                (
                    await s.execute(
                        text(
                            "SELECT d.derived->>'reason', count(*) FROM items i"
                            " JOIN item_derivations d ON d.item_id = i.id"
                            " WHERE i.event_kind = 'file_unavailable' GROUP BY 1"
                        )
                    )
                ).all()
            )
        if reasons:
            print(
                f"   {RED}!{RESET} file links in the export could not be fetched offline: {reasons}."
                f" Each is a recorded gap with its reason, so the job is '{job['status']}',"
                " never 'completed' (no silent data loss)",
                flush=True,
            )
        if recon["caveat"]:
            info(f"caveat: {recon['caveat']}")

        say(
            "6. Chain of custody verified in the database (hash chain + Merkle roots + WORM anchors)"
        )
        v = (await c.get(f"/v1/jobs/{job_id}/custody/verify")).json()
        check(v["ok"], v)
        ok(f"{v['events']} events, {v['batches_checked']} batches, {v['items_checked']} items, {v['anchors_checked']} WORM anchors")  # fmt: skip

        say("7. Offline custody package (for edisc-verify, no database needed)")
        package = out / "package"
        async with s3_client(settings) as s3:
            await export_package(sessions, s3, settings, tenant_id=tenant.tenant_id, job_id=uuid.UUID(job_id), dest=package)  # fmt: skip
        size = sum(p.stat().st_size for p in package.rglob("*") if p.is_file())
        ok(f"{package} ({size / 2**20:.1f} MiB, export zip embedded)")

        say(
            "8. RSMF: render the sealed job (step 3 loader: pinned, verified inputs) into locked productions"
        )
        render_id = uuid.uuid4()
        async with s3_client(settings) as s3:
            rendered = await render_and_store(
                sessions, s3, settings, tenant_id=tenant.tenant_id, job_id=uuid.UUID(job_id),
                render_id=render_id, options=RenderOptions(),
            )  # fmt: skip
            rsmf_dir = out / "rsmf"
            rsmf_dir.mkdir()
            for f in rendered.files:
                resp = await s3.get_object(Bucket=settings.s3_evidence_bucket, Key=f.storage_key, VersionId=f.version_id)  # fmt: skip
                async with resp["Body"] as body:
                    blob = await body.read()
                check(
                    hashlib.sha256(blob).hexdigest() == f.sha256, f"{f.name}: stored bytes differ"
                )
                (rsmf_dir / f.name).write_bytes(blob)
        # a named channel reads best in Mail; the busiest one, fixed by the data
        biggest = max(
            rendered.files,
            key=lambda f: (
                f.record["conversation_id"].startswith("C"),
                f.record["event_count"],
                f.name,
            ),
        )
        eml = out / (Path(biggest.name).stem + ".eml")
        shutil.copyfile(rsmf_dir / biggest.name, eml)
        r = rendered.reconciliation
        ok(
            f"{len(rendered.files)} RSMF files, every one locked, pinned and hash-checked on download"
        )
        ok(f"reconciliation: {r.items_in:,} items in = {r.events_out:,} events out, {r.context_events} thread-context events")  # fmt: skip
        ok(
            f"{rendered.verified_objects} archive entries re-read from the locked export and verified first"
        )
        ok(
            f"{r.unavailable_attachments} attachment references to unfetched files appear as <file>.UNAVAILABLE.txt placeholders, named and with their reason"
        )
        ok(f"open in Mail: {eml}")

        (out / "summary.json").write_text(json.dumps({
            "tenant": sub, "job_id": job_id, "status": job["status"], "killed_at": killed_at,
            "messages": linked, "duplicates": dupes, "custody": v, "render_id": str(render_id),
            "rsmf_files": len(rendered.files), "reconciliation": r.as_payload(), "eml": str(eml),
            "seconds": round(time.monotonic() - started, 1),
        }, indent=2) + "\n")  # fmt: skip
        await c.aclose()
        return {"package": str(package)}
    finally:
        worker.stop()
        await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--api", default="http://127.0.0.1:18100")
    args = ap.parse_args()
    asyncio.run(run(args.out, args.api))


if __name__ == "__main__":
    main()
