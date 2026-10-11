"""Offline recomputation of a report's chain-derived content (ADR 0018 §14, §27). PURE (no DB, S3 or
clock: the purity test covers it); `edisc-verify --job-package` calls it.

The report states facts from the verified job chain first (§7). Some of a `units.jsonl` row comes
from the database (kind, scopes, the access-lost reason), so the FILES cannot be recomputed byte for
byte offline. What can be, and is, is their chain-derived content: ``chain_projection`` folds the job
package's events with the report's own model (`ChainFold`, `unit_fact_from_event`) into one canonical
document; ``report_projection`` reads the same facts back from the report (`report.json` and every
`units.jsonl` row stated from the chain); ``compare`` requires the two to be byte-identical (RFC
8785) and names what differs:

- ``units``: every unit's chain fact (outcome, recon status, expected, collected, file gaps,
  no-longer-observed, basis, day anomalies, error type and text), one per unit key (the first event
  when the chain holds several, as the report states it);
- ``job``: who started it, the final status (when the chain has a final event), the unit day zone;
- ``scopes``, ``access`` (connector, version, connection, plan tier, granted scopes, blind spots,
  export) as `job_started` recorded them; ``pauses`` (with who re-authorized) and the count never
  resumed; ``actors``: every custody event's actor by type.

Not recomputable offline, and said so by the verifier: the database-sourced columns, the
observations, renders and conversations files, the snapshot (retention gaps, lock settings, the audit
events) and the S3 observation: those are checked against the digests recorded in `report.json` and
`report_generated` only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from edisc_core.canonical import canonical_json
from edisc_renderers.report.model import (
    UNKNOWN,
    ChainEvent,
    ChainFold,
    UnitFact,
    exact_document,
    or_unknown,
)

SECTIONS = ("units", "job", "scopes", "access", "pauses", "actors")
UNIT_FIELDS = tuple(UnitFact("", "reconciled", "").record())


def _when(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def chain_projection(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """From the job package's ``events.jsonl`` records (``{"fields": ...}``), in seq order: the
    chain-derived content a report of this job must state."""
    fold = ChainFold()
    first: dict[str, dict[str, Any]] = {}
    for rec in events:
        f = rec["fields"]
        fact = fold.add(
            ChainEvent(
                int(f["seq"]),
                str(f["event_type"]),
                str(f["actor"]),
                _when(f["created_at"]),
                dict(f.get("payload") or {}),
            )
        )
        if fact is not None:
            first.setdefault(fact.unit_key, fact.record())
    started = fold.started
    sp = dict(started.payload) if started is not None else {}
    return {
        "units": [first[k] for k in sorted(first)],
        "job": {
            "requested_by": started.actor if started is not None else UNKNOWN,
            "final_status": fold.final_status,
            "unit_day_zone": sp.get("unit_day_zone"),
        },
        "scopes": list(sp.get("scopes", [])),
        "access": _access(sp),
        "pauses": {
            "pauses": [p.record() for p in fold.pauses],
            "never_resumed": sum(1 for p in fold.pauses if p.resumed_at is None),
        },
        "actors": [
            {"event_type": t, "actor": a, "events": n} for (t, a), n in sorted(fold.actors.items())
        ],
    }


def _access(sp: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "connector": or_unknown(sp.get("connector")),
        "connector_version": or_unknown(sp.get("connector_version")),
        "connection_id": or_unknown(sp.get("connection_id")),
        "plan_tier": or_unknown(sp.get("plan_tier")),
        "granted_scopes": or_unknown(sp.get("granted_scopes")),
        "blind_spots": or_unknown(sp.get("blind_spots")),
        "export": sp.get("export"),
    }


def _unit_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A `units.jsonl` row stated from the chain, as its chain fact (`UnitFact.record`)."""
    return {
        **{k: row.get(k) for k in UNIT_FIELDS},
        "outcome": "failed" if row.get("status") == "failed" else "reconciled",
    }


def report_projection(report_json: bytes, units_lines: Iterable[bytes]) -> dict[str, Any]:
    """The same content as the report states it: `report.json` (with its exact strings back) and
    every `units.jsonl` row whose source is the chain (rows of unsettled units come from the database
    and have no chain fact). Rows are written valid (§23): their text fields are compared as written,
    which is how the chain records them too."""
    doc = exact_document(json.loads(report_json))
    units: dict[str, dict[str, Any]] = {}
    for raw in units_lines:
        row = json.loads(raw)
        if row.get("source") == "job_chain":
            units.setdefault(str(row["unit_key"]), _unit_from_row(row))
    job = doc.get("job", {})
    missing_final = any(
        d.get("kind") == "job_final_event_missing"
        for d in doc.get("divergences", {}).get("divergences", [])
    )
    access = doc.get("access", {})
    pauses = doc.get("pauses", {})
    return {
        "units": [units[k] for k in sorted(units)],
        "job": {
            "requested_by": job.get("requested_by"),
            "final_status": None if missing_final else job.get("status"),
            "unit_day_zone": job.get("unit_day_zone")
            if job.get("unit_day_zone_source") == "job_chain"
            else None,
        },
        "scopes": list(doc.get("scopes", {}).get("scopes", [])),
        "access": {
            k: access.get(k)
            for k in (
                "connector",
                "connector_version",
                "connection_id",
                "plan_tier",
                "granted_scopes",
                "blind_spots",
                "export",
            )
        },
        "pauses": {
            "pauses": list(pauses.get("pauses", [])),
            "never_resumed": pauses.get("never_resumed"),
        },
        "actors": list(doc.get("actors", {}).get("custody", [])),
    }


def compare(chain: Mapping[str, Any], report: Mapping[str, Any]) -> list[str]:
    """Byte-compare the two projections (RFC 8785); on a difference, name each differing section and
    up to five differing units."""
    if canonical_json(chain) == canonical_json(report):
        return []
    out = []
    for section in SECTIONS:
        if canonical_json(chain.get(section)) == canonical_json(report.get(section)):
            continue
        if section != "units":
            out.append(
                f"{section}: the report states {_short(report.get(section))}, the chain"
                f" says {_short(chain.get(section))}"
            )
            continue
        mine = {u["unit_key"]: u for u in report.get("units", [])}
        theirs = {u["unit_key"]: u for u in chain.get("units", [])}
        keys = sorted(k for k in mine.keys() | theirs.keys() if mine.get(k) != theirs.get(k))
        out.extend(
            f"unit {k}: the report states {_short(mine.get(k))}, the chain says"
            f" {_short(theirs.get(k))}"
            for k in keys[:5]
        )
        if len(keys) > 5:
            out.append(f"... and {len(keys) - 5} more units differ")
    return out


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    return text if len(text) <= 300 else text[:297] + "..."
