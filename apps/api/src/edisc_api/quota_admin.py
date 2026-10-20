"""Operator command: set a tenant's export upload quotas (M18 §4, D10). Never an API route: tenant
admins read their quotas (``GET /v1/tenant/quotas``) but cannot raise them.

    uv run python -m edisc_api.admin quota --tenant <uuid> --in-flight-bytes N --stored-bytes N \
        --concurrent N --reason "..." --operator <name>

Runs with the OWNER credentials (an operator host): the app role may only read ``tenant_quotas``.
Every change is a ``tenant_quota_changes`` row (before, after, who, why) and an
``audit.export_quota_set`` event in the tenant's stream.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from edisc_api import audit
from edisc_api.quotas import Quota
from edisc_core.ids import new_id
from edisc_db.session import tenant_tx


class QuotaRefusedError(RuntimeError):
    pass


async def set_quota(
    owner: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    quota: Quota,
    reason: str,
    operator: str,
) -> Quota | None:
    """Returns the previous quota (None: the defaults applied)."""
    if not reason.strip():
        raise QuotaRefusedError("a reason is required")
    if min(asdict(quota).values()) <= 0:
        raise QuotaRefusedError("every limit must be positive")
    async with tenant_tx(owner, tenant_id) as s:
        row = (
            await s.execute(
                text(
                    "SELECT export_bytes_in_flight, export_bytes_stored, export_uploads_concurrent"
                    " FROM tenant_quotas WHERE tenant_id = :t FOR UPDATE"
                ),
                {"t": tenant_id},
            )
        ).one_or_none()
        before = Quota(**row._mapping) if row is not None else None
        await s.execute(
            text(
                "INSERT INTO tenant_quotas (tenant_id, export_bytes_in_flight, export_bytes_stored,"
                " export_uploads_concurrent) VALUES (:t, :f, :s, :c)"
                " ON CONFLICT (tenant_id) DO UPDATE SET export_bytes_in_flight = EXCLUDED.export_bytes_in_flight,"
                " export_bytes_stored = EXCLUDED.export_bytes_stored,"
                " export_uploads_concurrent = EXCLUDED.export_uploads_concurrent, updated_at = now()"
            ),
            {"t": tenant_id, "f": quota.export_bytes_in_flight, "s": quota.export_bytes_stored,
             "c": quota.export_uploads_concurrent},
        )  # fmt: skip
        payload = {
            "before": asdict(before) if before else None,
            "after": asdict(quota),
            "reason": reason,
        }
        await s.execute(
            text(
                "INSERT INTO tenant_quota_changes (id, tenant_id, operator, reason, before, after)"
                " VALUES (:i, :t, :o, :r, CAST(:b AS jsonb), CAST(:a AS jsonb))"
            ),
            {"i": new_id(), "t": tenant_id, "o": f"operator:{operator}", "r": reason,
             "b": None if before is None else json.dumps(asdict(before)),
             "a": json.dumps(asdict(quota))},
        )  # fmt: skip
        await audit.record(
            s, tenant_id=tenant_id, actor=f"operator:{operator}", event_type="export_quota_set",
            payload=payload,
        )  # fmt: skip
    return before


def main(argv: list[str]) -> None:
    from edisc_core.settings import Settings
    from edisc_db.session import create_engine, session_factory

    ap = argparse.ArgumentParser(prog="edisc_api.admin quota", description=__doc__)
    ap.add_argument("--tenant", required=True, type=uuid.UUID)
    ap.add_argument("--in-flight-bytes", required=True, type=int)
    ap.add_argument("--stored-bytes", required=True, type=int)
    ap.add_argument("--concurrent", required=True, type=int)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--operator", required=True)
    args = ap.parse_args(argv)
    settings = Settings()

    async def run() -> None:
        owner = create_engine(settings, "owner")
        try:
            before = await set_quota(
                session_factory(owner), tenant_id=args.tenant, reason=args.reason,
                operator=args.operator,
                quota=Quota(args.in_flight_bytes, args.stored_bytes, args.concurrent),
            )  # fmt: skip
            sys.stdout.write(f"quota set for {args.tenant} (before: {before})\n")
        finally:
            await owner.dispose()

    asyncio.run(run())
