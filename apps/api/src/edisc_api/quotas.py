"""Per-tenant Slack export upload quotas (M18 §4, D10; migration 0046).

Limits: the tenant's ``tenant_quotas`` row (set by the operator CLI only), else the settings'
defaults. Usage is COMPUTED from ``slack_exports`` inside the create transaction, after a
per-tenant transaction advisory lock: two concurrent creates at the limit serialize on the lock,
and the second sees the first's committed row (READ COMMITTED: each statement takes a new
snapshot). Nothing to drift, nothing to release by hand: a rejected or expired upload simply stops
counting (the ``expire-export-uploads`` sweep rejects uploads past their TTL).

- in flight: declared sizes of exports ``uploading`` / ``locking`` / ``validating``;
- stored: sizes of ``ready`` exports of open clients, plus in flight;
- concurrent: the number of exports in flight.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edisc_api.errors import ApiError
from edisc_core.settings import Settings

# pg_advisory_xact_lock(key1, key2): key1 names this lock family (no other user of 2-int keys)
LOCK_NAMESPACE = 0x45515554  # "EQUT"
IN_FLIGHT = ("uploading", "locking", "validating")


@dataclass(frozen=True)
class Quota:
    export_bytes_in_flight: int
    export_bytes_stored: int
    export_uploads_concurrent: int


@dataclass(frozen=True)
class Usage:
    export_bytes_in_flight: int
    export_bytes_stored: int
    export_uploads_concurrent: int


async def limits(s: AsyncSession, settings: Settings, tenant_id: uuid.UUID) -> Quota:
    row = (
        await s.execute(
            text(
                "SELECT export_bytes_in_flight, export_bytes_stored, export_uploads_concurrent"
                " FROM tenant_quotas WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
    ).one_or_none()
    if row is not None:
        return Quota(**row._mapping)
    return Quota(
        settings.export_quota_bytes_in_flight,
        settings.export_quota_bytes_stored,
        settings.export_quota_uploads_concurrent,
    )


async def usage(s: AsyncSession) -> Usage:
    row = (
        await s.execute(
            text(
                "SELECT coalesce(sum(x.declared_size) FILTER (WHERE x.status = ANY(:f)), 0) AS f,"
                " count(*) FILTER (WHERE x.status = ANY(:f)) AS n,"
                " coalesce(sum(x.size_bytes) FILTER (WHERE x.status = 'ready'"
                "   AND c.closed_at IS NULL), 0) AS r"
                " FROM slack_exports x JOIN clients c ON c.id = x.client_id"
                " WHERE x.status = ANY(:f) OR x.status = 'ready'"
            ),
            {"f": list(IN_FLIGHT)},
        )
    ).one()
    return Usage(int(row.f), int(row.f) + int(row.r), int(row.n))


async def reserve(
    s: AsyncSession, settings: Settings, tenant_id: uuid.UUID, size: int
) -> tuple[Quota, Usage]:
    """Take the tenant's quota lock (held to the end of the transaction, which must insert the
    export) and refuse when ``size`` more would exceed any limit. 409 ``quota_exceeded``: the
    detail names the limit and the usage (server state), never the size the request declared."""
    await s.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(CAST(:t AS text)))"),
        {"ns": LOCK_NAMESPACE, "t": str(tenant_id)},
    )
    quota, used = await limits(s, settings, tenant_id), await usage(s)
    over = [
        name
        for name, limit, now, add in (
            ("export_uploads_concurrent", quota.export_uploads_concurrent,
             used.export_uploads_concurrent, 1),
            ("export_bytes_in_flight", quota.export_bytes_in_flight, used.export_bytes_in_flight, size),
            ("export_bytes_stored", quota.export_bytes_stored, used.export_bytes_stored, size),
        )
        if now + add > limit
    ]  # fmt: skip
    if over:
        raise QuotaExceededError(quota, used, over)
    return quota, used


class QuotaExceededError(ApiError):
    def __init__(self, quota: Quota, used: Usage, over: list[str]) -> None:
        detail = "; ".join(
            f"{name}: limit {getattr(quota, name)}, in use {getattr(used, name)}" for name in over
        )
        super().__init__(409, "quota_exceeded", f"the export upload quota is used up ({detail})")
        self.quota, self.used, self.over = quota, used, over

    def audit_payload(self) -> dict[str, object]:
        return {"over": self.over, "limits": asdict(self.quota), "usage": asdict(self.used)}
