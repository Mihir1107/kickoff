"""Retention policy for WORM writes (ADR 0002): a short rolling COMPLIANCE window, never long upfront.

COMPLIANCE retention can be extended but never shortened. Locking evidence for the matter's full
retention at write time would make a client's destruction request at matter close impossible to
honour. So every object gets ``min(matter.retention_until, now + window)``, and a matter-level
extension job pushes it forward while the matter is active (backlog, required before production).
When the matter closes, extension stops and objects expire on schedule.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now


def effective_retain_until(
    settings: Settings, matter_retention_until: datetime | None = None
) -> datetime:
    """Retain-until for a new object: rolling window, capped by the matter's date, then the local/ci cap."""
    now = utc_now()
    target = now + timedelta(days=settings.evidence_retention_window_days)
    if matter_retention_until is not None:
        matter_until = ensure_utc(matter_retention_until)
        if matter_until <= now:
            raise ValueError(
                f"matter retention ended at {matter_until.isoformat()}; refusing to collect"
            )
        target = min(target, matter_until)
    override = settings.evidence_retention_override_days
    if override is not None:
        if (
            not settings.env.is_disposable
        ):  # settings validation already forbids this; defense in depth
            raise RuntimeError("retention override outside local/ci")
        target = min(target, now + timedelta(days=override))
    return target
