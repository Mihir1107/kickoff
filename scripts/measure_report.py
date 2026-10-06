"""Collection report loader at scale (ADR 0018 §12): peak memory must stay flat from 10k to 100k units.

Collecting 100,000 conversation-days through the pipeline would take hours on a laptop, so this
builds a SYNTHETIC sealed job directly: work-unit rows and a real hash chain (`job_started`, one
`unit_reconciled` per unit through `edisc_custody.log.append`, `job_finished`, the WORM seal). The
report then reads it exactly as it reads a collected job: verification, chain pass, database pass,
every file streamed. Peak memory is traced (tracemalloc) around the build only.

    EDISC_ENV_FILE=.env.test uv run python scripts/measure_report.py --units 10000 100000

Run on the test stack (`make test-env-up`), never the dev stack. Results go in docs/runs/.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import tracemalloc
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta

from sqlalchemy import text

from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.log import append, seal_job_chain
from edisc_db.session import create_engine, create_tenant, session_factory, tenant_tx
from edisc_evidence.s3 import s3_client
from edisc_worker.report_loader import ReportLoader

PER_TX = 1_000
START = date(2026, 1, 5)


async def synthetic_job(
    sessions, s3, settings: Settings, units: int
) -> tuple[uuid.UUID, uuid.UUID]:  # type: ignore[no-untyped-def]
    tenant, matter, conn, job = new_id(), new_id(), new_id(), new_id()
    await create_tenant(sessions, tenant_id=tenant, name="M", subdomain=f"m-{tenant.hex[:12]}",
                        kms_key_ref="local:k")  # fmt: skip
    keys = [
        f"C{i // 100:06d}/{(START + timedelta(days=i % 100)).isoformat()}" for i in range(units)
    ]
    async with tenant_tx(sessions, tenant) as s:
        await s.execute(text("INSERT INTO matters (id, tenant_id, name, retention_until)"
                             " VALUES (:m, :t, 'M', now() + interval '30 days')"),
                        {"m": matter, "t": tenant})  # fmt: skip
        await s.execute(text("INSERT INTO connections (id, tenant_id, source, external_org_id,"
                             " status) VALUES (:c, :t, 'dummy', 'org', 'active')"),
                        {"c": conn, "t": tenant})  # fmt: skip
        await s.execute(text("INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id,"
                             " status, connector_version, requested_by) VALUES (:j, :t, :m, :c,"
                             " 'running', '0.4.0', 'measure')"),
                        {"j": job, "t": tenant, "m": matter, "c": conn})  # fmt: skip
        await append(s, tenant_id=tenant, stream_id=job, job_id=job, event_type="job_started",
                     actor="measure", payload={"connector": "dummy", "connector_version": "0.4.0",
                     "connection_id": str(conn), "scopes": [], "unit_day_zone": "UTC"})  # fmt: skip
    for start in range(0, units, PER_TX):
        chunk = keys[start : start + PER_TX]
        async with tenant_tx(sessions, tenant) as s:
            await s.execute(
                text(
                    "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day,"
                    " status, expected_count, collected_count, recon_status)"
                    " SELECT :t, :j, k, split_part(k, '/', 1), CAST(split_part(k, '/', 2) AS date),"
                    " 'done', 5, 5, 'matched' FROM unnest(CAST(:k AS text[])) AS k"
                ),
                {"t": tenant, "j": job, "k": chunk},
            )
            for key in chunk:
                await append(s, tenant_id=tenant, stream_id=job, job_id=job,
                             event_type="unit_reconciled", actor="measure",
                             payload={"unit_key": key, "expected": 5, "collected": 5,
                                      "recon_status": "matched", "file_gaps": 0,
                                      "no_longer_observed": 0},
                             anchor_every=1 << 30)  # fmt: skip
    async with tenant_tx(sessions, tenant) as s:
        await append(s, tenant_id=tenant, stream_id=job, job_id=job, event_type="job_finished",
                     actor="measure", payload={"status": "completed", "units": {"matched": units},
                     "paused_ms": 0, "stop_reason": None})  # fmt: skip
        await s.execute(text("UPDATE collection_jobs SET status = 'completed', finished_at = now()"
                             " WHERE id = :j"), {"j": job})  # fmt: skip
    await seal_job_chain(sessions, s3, settings, tenant_id=tenant, job_id=job)
    async with tenant_tx(sessions, tenant) as s:
        await s.execute(text("UPDATE collection_jobs SET sealed_at = now() WHERE id = :j"),
                        {"j": job})  # fmt: skip
    return tenant, job


async def measure(units: int) -> dict[str, float]:
    settings = Settings()
    engine = create_engine(settings, "app")
    sessions = session_factory(engine)
    try:
        async with s3_client(settings) as s3:
            t0 = time.monotonic()
            tenant, job = await synthetic_job(sessions, s3, settings, units)
            built_at = time.monotonic()
            sizes: dict[str, int] = {}

            async def sink(name: str, chunks: AsyncIterator[bytes]) -> None:
                sizes[name] = 0
                async for chunk in chunks:
                    sizes[name] += len(chunk)

            tracemalloc.start()
            report = await ReportLoader(sessions, s3, settings, tenant_id=tenant, job_id=job).build(
                sink
            )
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            done = time.monotonic()
            if not report.document["job"]["clean"] or report.files[0].rows != units:
                raise SystemExit(f"unexpected report: {report.document['banner']}")
    finally:
        await engine.dispose()
    return {"units": units, "setup_s": built_at - t0, "report_s": done - built_at,
            "peak_mib": peak / 2**20, "units_jsonl_mib": sizes["units.jsonl"] / 2**20}  # fmt: skip


async def main(counts: list[int]) -> None:
    results = [await measure(n) for n in counts]
    for r in results:
        print(f"{r['units']:>9,} units: report {r['report_s']:6.1f} s, peak {r['peak_mib']:6.1f} MiB,"
              f" units.jsonl {r['units_jsonl_mib']:6.1f} MiB (setup {r['setup_s']:.0f} s)")  # fmt: skip
    if len(results) >= 2:
        small, big = results[0], results[-1]
        growth = big["peak_mib"] / max(small["peak_mib"], 1e-9)
        print(f"peak memory x{growth:.2f} for x{big['units'] / small['units']:.0f} units")
        if growth >= 2.0:
            raise SystemExit("report memory is not flat in the number of units (ADR 0018 §12)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--units", type=int, nargs="+", default=[10_000, 100_000])
    asyncio.run(main(ap.parse_args().units))
