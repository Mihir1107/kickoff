"""Measure custody head-lock contention: W concurrent writers committing batch transactions to ONE job
chain vs W separate chains. Each transaction mirrors the M11 batch commit (items + batch event + job_items).

    uv run python scripts/bench_custody_contention.py --writers 1 4 16 32 --batches 40 --items 100
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import statistics
import sys
import time
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "integration"))
from custody.conftest import new_job

from edisc_core.canonical import canonical_hash
from edisc_core.idempotency import idempotency_key
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.log import append_batch
from edisc_db.session import create_engine, session_factory, tenant_tx


async def batch_tx(sessions, job, ev_id: object, unit: str, n: int) -> float:  # type: ignore[no-untyped-def]
    start = time.perf_counter()
    async with tenant_tx(sessions, job.tenant_id) as s:
        rows, pairs = [], []
        for _ in range(n):
            ch = canonical_hash({"t": secrets.token_hex(8)})
            sid = f"W/C/{secrets.token_hex(8)}"
            ik = idempotency_key(job.tenant_id, "dummy", sid, ch)
            rows.append(
                {
                    "id": new_id(),
                    "t": job.tenant_id,
                    "j": job.job_id,
                    "sid": sid,
                    "ch": ch,
                    "ev": ev_id,
                    "ik": ik,
                }
            )
            pairs.append((ik, ch))
        await s.execute(
            text(
                "INSERT INTO items (id, tenant_id, job_id, source, source_item_id, version, item_type, content_hash, raw_hash,"
                " evidence_object_id, storage_key, json_path, connector_version, normalizer_version, idempotency_key)"
                " VALUES (:id, :t, :j, 'dummy', :sid, 1, 'message', :ch, :ch, :ev, 'k', '$', '0', '0', :ik)"
            ),
            rows,
        )
        event = await append_batch(
            s,
            tenant_id=job.tenant_id,
            job_id=job.job_id,
            unit_key=unit,
            page_evidence_id=ev_id,
            page_sha256="ab" * 32,
            items=pairs,
            actor="bench",
        )  # type: ignore[arg-type]
        await s.execute(
            text(
                "INSERT INTO job_items (tenant_id, job_id, item_id, unit_key, custody_event_id) VALUES (:t, :j, :i, :u, :e)"
            ),
            [
                {"t": job.tenant_id, "j": job.job_id, "i": r["id"], "u": unit, "e": event.id}
                for r in rows
            ],
        )
    return time.perf_counter() - start


async def prepare(sessions, job, writers: int):  # type: ignore[no-untyped-def]
    ev_id = new_id()
    async with tenant_tx(sessions, job.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO evidence_objects (id, tenant_id, job_id, storage_key, kind, retain_until, state, sha256,"
                " size_bytes, completed_at, version_id, source_sha256, source_hash_origin) VALUES (:id, :t, :j, :k,"
                " 'page', now() + interval '1 day', 'complete', :h, 1, now(), 'bench', :h, 'collection')"
            ),
            {
                "id": ev_id,
                "t": job.tenant_id,
                "j": job.job_id,
                "k": f"bench/{ev_id}",
                "h": "ab" * 32,
            },
        )
        for w in range(writers):
            await s.execute(
                text(
                    "INSERT INTO work_units (tenant_id, job_id, unit_key, conversation_id, day)"
                    " VALUES (:t, :j, :u, 'C', '2026-01-01')"
                ),
                {"t": job.tenant_id, "j": job.job_id, "u": f"C/{w}"},
            )
    return ev_id


async def scenario(
    sessions, writers: int, batches: int, items: int, shared: bool
) -> dict[str, float]:  # type: ignore[no-untyped-def]
    jobs = (
        [await new_job(sessions)] if shared else [await new_job(sessions) for _ in range(writers)]
    )
    evs = [await prepare(sessions, j, writers) for j in jobs]
    latencies: list[float] = []

    async def writer(w: int) -> None:
        i = 0 if shared else w
        for _ in range(batches):
            latencies.append(await batch_tx(sessions, jobs[i], evs[i], f"C/{w}", items))

    start = time.perf_counter()
    await asyncio.gather(*(writer(w) for w in range(writers)))
    wall = time.perf_counter() - start
    latencies.sort()
    return {
        "batches_per_s": writers * batches / wall,
        "msgs_per_s": writers * batches * items / wall,
        "p50_ms": statistics.median(latencies) * 1000,
        "p95_ms": latencies[int(len(latencies) * 0.95) - 1] * 1000,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--writers", type=int, nargs="+", default=[1, 4, 16, 32])
    ap.add_argument("--batches", type=int, default=40)
    ap.add_argument("--items", type=int, default=100)
    args = ap.parse_args()
    engine = create_engine(Settings(), "app", pool_size=max(args.writers) + 4)
    sessions = session_factory(engine)
    print("| writers | chain | batches/s | msgs/s | p50 ms | p95 ms |\n|---|---|---|---|---|---|")
    for w in args.writers:
        for shared in (True, False):
            r = await scenario(sessions, w, args.batches, args.items, shared)
            print(
                f"| {w} | {'one shared' if shared else 'one per writer'} | {r['batches_per_s']:.0f} | {r['msgs_per_s']:.0f} |"
                f" {r['p50_ms']:.1f} | {r['p95_ms']:.1f} |",
                flush=True,
            )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
