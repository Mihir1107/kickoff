"""Burst of audited evidence downloads against the tenant-wide audit chain (measurement only).

    make test-env-up
    EDISC_ENV_FILE=.env.test uv run python scripts/measure_audit_burst.py --concurrency 50 200

Every content read appends ``audit.evidence_content_read`` to the tenant's custody stream (one chain per
tenant, ADR 0013) before streaming. Appends to one stream serialize on its chain-head row lock. This
runs N concurrent downloads through the real API (in-process ASGI, real Postgres/MinIO) twice:

- audited (production behaviour);
- with the audit append replaced by a no-op (baseline: the cost of the download itself);

and reports latency percentiles, wall time, and how long requests waited for the chain head.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as aioredis
from sqlalchemy import text

import edisc_api.audit as audit_mod
from edisc_api.admin import onboard_tenant
from edisc_api.app import Resources, create_app
from edisc_api.auth import DEV_ISSUER, DEV_JWKS, Authenticator, JwksCache, dev_token
from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.ratelimit import RateLimiter
from edisc_connectors_base.types import Connection
from edisc_core.envelope import SecretBox
from edisc_core.ids import new_id
from edisc_core.kms import LocalKmsClient
from edisc_core.settings import RateLimitConfig, Settings
from edisc_custody.log import append
from edisc_db.session import create_engine, session_factory, tenant_tx
from edisc_evidence.s3 import s3_client
from edisc_worker.pipeline import Pipeline

AUDIENCE = "edisc-api"


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, nargs="+", default=[50])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    settings = Settings().model_copy(update={"api_dev_idp": True})
    engine = create_engine(settings, "app")  # production pool size (10 + 10 overflow)
    sessions = session_factory(engine)
    redis = aioredis.from_url(settings.redis_url)
    kms = LocalKmsClient(settings)
    http = httpx.AsyncClient()
    results: dict[str, Any] = {}
    async with s3_client(settings) as s3:
        limiter = RateLimiter(
            redis,
            {k: RateLimitConfig(rate_per_second=10_000, burst=1000) for k in settings.rate_limits},
        )
        resources = Resources(
            settings, sessions, s3, None,  # type: ignore[arg-type]  # no Temporal needed for reads
            limiter, SecretBox(kms), Authenticator(settings, sessions, JwksCache(settings, http)),
            {"dummy": DummyConnector(limiter)}, None,
        )  # fmt: skip
        # one tenant, one matter, one finished job with page evidence
        sub = f"burst{secrets.token_hex(4)}"
        kms.create_key(f"tenant/{sub}")
        admin_sub = "admin"
        t = await onboard_tenant(
            sessions, kms_key_ref=f"tenant/{sub}", subdomain=sub, name="Burst", issuer=DEV_ISSUER,
            audience=AUDIENCE, jwks_url=DEV_JWKS, admin_subject=admin_sub, admin_name="A", operator="measure",
        )  # fmt: skip
        sp = DatasetSpec(seed=3, conversations=4, days=3, messages_per_unit=60, page_size=20)
        ds = Dataset(sp)
        matter, conn_id, job = new_id(), new_id(), new_id()
        async with tenant_tx(sessions, t.tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES (:m, :t, 'M', :r)"
                ),
                {"m": matter, "t": t.tenant_id, "r": datetime.now(UTC) + timedelta(days=30)},
            )
            await s.execute(
                text(
                    "INSERT INTO connections (id, tenant_id, source, external_org_id, status, config)"
                    " VALUES (:c, :t, 'dummy', :w, 'active', CAST(:cfg AS jsonb))"
                ),
                {
                    "c": conn_id,
                    "t": t.tenant_id,
                    "w": sp.workspace_id,
                    "cfg": json.dumps({"spec": sp.model_dump(mode="json")}),
                },
            )
        p = Pipeline(sessions, s3, settings, DummyConnector(limiter))
        start = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
        await p.start_job(
            tenant_id=t.tenant_id, job_id=job, matter_id=matter, connection_id=conn_id,
            scopes=[scope_for_days("*", start, ds.n_days(0))], requested_by="measure",
        )  # fmt: skip
        conn = Connection(
            t.tenant_id, conn_id, "dummy", sp.workspace_id, {"spec": sp.model_dump(mode="json")}
        )
        await p.run(tenant_id=t.tenant_id, job_id=job, conn=conn, max_pages=50)
        async with tenant_tx(sessions, t.tenant_id) as s:
            evidence = list(
                (
                    await s.execute(
                        text(
                            "SELECT id FROM evidence_objects WHERE job_id = :j AND kind = 'page' AND state = 'complete'"
                        ),
                        {"j": job},
                    )
                ).scalars()
            )
        token = dev_token(settings, subject=admin_sub, audience=AUDIENCE)
        app = create_app(settings, resources)
        wait_for_head: list[float] = []
        original_record, original_anchor = audit_mod.record, audit_mod.anchor

        async def timed_record(*a: Any, **kw: Any) -> None:
            started = time.perf_counter()
            await original_record(*a, **kw)  # includes waiting for the chain-head row lock
            wait_for_head.append(time.perf_counter() - started)

        async def unshared_record(
            session: Any,
            *,
            tenant_id: Any,
            actor: str,
            event_type: str,
            payload: Any,
            request_id: str | None = None,
        ) -> None:
            """Same work, but each request appends to its own stream (no shared chain head): separates
            the cost of contention on the tenant's chain head from the cost of appending at all."""
            started = time.perf_counter()
            await append(session, tenant_id=tenant_id, stream_id=new_id(), event_type=f"audit.{event_type}",
                         actor=actor, payload=payload)  # fmt: skip
            wait_for_head.append(time.perf_counter() - started)

        async def no_record(*_: Any, **__: Any) -> None:
            return None

        async def no_anchor(*_: Any, **__: Any) -> None:
            return None

        async def burst(n: int) -> tuple[list[float], float]:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=f"http://{sub}.{settings.api_base_domain}",
                headers={"authorization": f"Bearer {token}"}, timeout=120,
            ) as c:  # fmt: skip

                async def one(i: int) -> float:
                    started = time.perf_counter()
                    r = await c.get(
                        f"/v1/evidence/{evidence[i % len(evidence)]}/content",
                        params={"purpose": "preview"},
                    )
                    r.raise_for_status()
                    return time.perf_counter() - started

                started = time.perf_counter()
                latencies = await asyncio.gather(*(one(i) for i in range(n)))
                return list(latencies), time.perf_counter() - started

        modes: dict[str, tuple[Callable[..., Awaitable[None]], Callable[..., Awaitable[None]]]] = {
            "audited": (timed_record, original_anchor),
            "audited_shared_stream_no_anchor": (timed_record, no_anchor),
            "audited_unshared_streams": (unshared_record, no_anchor),
            "baseline_no_audit": (no_record, no_anchor),
        }
        for n in args.concurrency:
            for mode, (rec, anc) in modes.items():
                audit_mod.record, audit_mod.anchor = rec, anc  # type: ignore[assignment]
                lat_all: list[float] = []
                walls: list[float] = []
                wait_for_head.clear()
                await burst(min(n, 10))  # warm-up (pool, JWKS, S3 connections)
                wait_for_head.clear()
                for _ in range(args.rounds):
                    lat, wall = await burst(n)
                    lat_all += lat
                    walls.append(wall)
                results[f"{mode}@{n}"] = {
                    "requests": len(lat_all),
                    "p50_ms": round(1000 * statistics.median(lat_all), 1),
                    "p95_ms": round(1000 * pct(lat_all, 0.95), 1),
                    "max_ms": round(1000 * max(lat_all), 1),
                    "wall_s_per_burst": round(statistics.median(walls), 2),
                    "req_per_s": round(n / statistics.median(walls), 1),
                    **(
                        {
                            "audit_append_p50_ms": round(
                                1000 * statistics.median(wait_for_head), 1
                            ),
                            "audit_append_p95_ms": round(1000 * pct(wait_for_head, 0.95), 1),
                            "audit_append_max_ms": round(1000 * max(wait_for_head), 1),
                        }
                        if wait_for_head
                        else {}
                    ),
                }
        audit_mod.record, audit_mod.anchor = original_record, original_anchor  # type: ignore[assignment]
    await http.aclose()
    await redis.aclose()
    await engine.dispose()
    out = json.dumps(results, indent=2)
    sys.stdout.write(out + "\n")
    if args.out:
        args.out.write_text(out)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
