"""Inputs to byte identity besides the data and `RENDERER_VERSION` (ADR 0015 §6):

- time zones come ONLY from the pinned `tzdata` package, never the system zoneinfo (which differs per
  machine and changes with OS updates);
- Unicode data (NFC, character categories for zip names) comes from the interpreter, pinned via
  `.python-version`.

Both versions are recorded in the render summary, the render's custody stream and the golden key.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, tzinfo
from functools import cache
from importlib import resources
from zoneinfo import ZoneInfo

import tzdata  # type: ignore[import-untyped]  # data package without type hints

from edisc_renderers.rsmf.version import RENDERER_VERSION

TZDATA_VERSION: str = str(tzdata.IANA_VERSION)
UNICODE_VERSION: str = unicodedata.unidata_version


class UnknownZoneError(ValueError):
    pass


@cache
def load_zone(name: str) -> tzinfo:
    """The IANA zone `name` from the pinned tzdata package. `UTC` is the stdlib UTC singleton."""
    if name == "UTC":
        return UTC
    parts = name.split("/")
    if not name or any(p in ("", ".", "..") or not p.replace("_", "").replace("-", "").replace("+", "").isalnum() for p in parts):  # fmt: skip
        raise UnknownZoneError(f"invalid time zone name {name!r}")
    resource = resources.files("tzdata.zoneinfo").joinpath(*parts)
    if not resource.is_file():
        raise UnknownZoneError(f"unknown time zone {name!r} (tzdata {TZDATA_VERSION})")
    with resource.open("rb") as fh:
        return ZoneInfo.from_file(fh, key=name)


def runtime_versions() -> dict[str, str]:
    return {
        "renderer_version": RENDERER_VERSION,
        "unicode_version": UNICODE_VERSION,
        "tzdata_version": TZDATA_VERSION,
    }


def golden_key() -> str:
    """Directory name of the golden generation: every input to byte identity except the data."""
    return f"{RENDERER_VERSION}_unicode-{UNICODE_VERSION}_tzdata-{TZDATA_VERSION}"
