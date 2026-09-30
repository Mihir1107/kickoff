"""Retention policy for WORM writes."""

from __future__ import annotations

from datetime import datetime, timedelta

from edisc_core.settings import Settings
from edisc_core.time import ensure_utc, utc_now


def effective_retain_until(settings: Settings, requested: datetime) -> datetime:
    """The retain-until to put on an object: the matter's date, capped only in local/ci (ADR 0002)."""
    requested = ensure_utc(requested)
    if requested <= utc_now():
        raise ValueError(f"retain-until must be in the future, got {requested.isoformat()}")
    override = settings.evidence_retention_override_days
    if override is not None:
        if (
            not settings.env.is_disposable
        ):  # settings validation already forbids this; defense in depth
            raise RuntimeError("retention override outside local/ci")
        return min(requested, utc_now() + timedelta(days=override))
    return requested
