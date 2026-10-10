"""The collection report model (ADR 0018 §1, §3, §4, §7, §8, §12). PURE: no DB, S3 or clock; the
worker loader (`edisc_worker.report_loader`) feeds it records, and the offline verifier imports it to
recompute the chain-derived part from a job custody package (§14).

What it decides, in one place each:
- what a unit's chain fact is (`unit_fact_from_event`) and how the database's row compares with it
  (`unit_fact_from_row`): the cross-check of §12 folds both into an additive digest over 4,096
  buckets (`DigestFold`); only differing buckets are compared unit by unit (`compare_units`);
- what the job chain says (`ChainFold`: `job_started`, unit events, pauses, the final status, actors);
- the rows of `units.jsonl`, `observations.jsonl`, `renders.jsonl`, `conversations.jsonl` and their
  order; their bytes (`jsonl_line`), digests (`JsonlDigest`);
- `clean` (§4.1): ONE function for every output;
- capped lists (§4.6): worst status first, then a stable key (`Capped`);
- `report.json` (`report_json`): every section with its `source`, every enum value with a row (zeros
  included), UNKNOWN where nothing was recorded, no report id, no generation time, no host name.
"""

from __future__ import annotations

import hashlib
import heapq
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from edisc_core.canonical import canonical_json
from edisc_core.schemas import ARCHIVE_CAVEAT, JobStatus, ReconStatus, UnitStatus
from edisc_core.time import format_utc
from edisc_renderers.report.version import REPORT_FORMAT, REPORT_RENDERER_VERSION

UNKNOWN = "UNKNOWN (not recorded)"
ZONE_NOT_RECORDED = "UTC (ADR 0005; not recorded in the chain)"
CAP = 1_000
BUCKETS = 4_096
_MOD = 1 << 256

# worst first (§4.6); a unit's severity is its recon status ("failed" for a failed unit)
SEVERITY: tuple[str, ...] = (
    "failed", "access_lost", "gap", "surplus", "unverifiable", "matched_against_archive",
    "pending", "matched", "not_applicable",
)  # fmt: skip
_RANK = {s: i for i, s in enumerate(SEVERITY)}
EXCEPTION_RECON = ("failed", "access_lost", "gap", "surplus", "unverifiable")

UNIT_EVENTS = ("unit_reconciled", "unit_failed")
FINAL_EVENTS = ("job_finished", "job_cancelled")
OBSERVATION_KINDS = (
    "file_unavailable", "file_became_available", "access_lost", "access_restored",
    "no_longer_observed", "observed_again",
)  # fmt: skip

Source = Literal["job_chain", "database", "snapshot", "not_recorded"]


def iso(value: datetime | None) -> str | None:
    return None if value is None else format_utc(value)


def jsonl_line(record: Mapping[str, Any]) -> bytes:
    """One JSONL row: RFC 8785 canonical JSON and LF (§3.1)."""
    return canonical_json(record) + b"\n"


def _leaf(record: Mapping[str, Any]) -> int:
    return int.from_bytes(hashlib.sha256(canonical_json(record)).digest(), "big")


def bucket_of(unit_key: str) -> int:
    """One of 4,096 buckets by SHA-256(unit key): the first 12 bits."""
    digest = hashlib.sha256(unit_key.encode("utf-8")).digest()
    return ((digest[0] << 4) | (digest[1] >> 4)) % BUCKETS


def day_label(unit_key: str) -> str | None:
    """The day as RECORDED in the unit key (`<conversation>/<YYYY-MM-DD>`, §3.3); no computation."""
    head, sep, tail = unit_key.rpartition("/")
    if not sep or not head:
        return None
    try:
        date.fromisoformat(tail)
    except ValueError:
        return None
    return tail


# ------------------------------------------------------------------ units: chain fact vs database row
@dataclass(frozen=True)
class UnitFact:
    """What the chain says about one unit (or, from a database row, what it should say). Equal
    records mean the database agrees with the chain."""

    unit_key: str
    outcome: Literal["reconciled", "failed"]
    recon_status: str
    expected: int | None = None
    collected: int | None = None
    file_gaps: int | None = None
    no_longer_observed: int | None = None
    basis: str | None = None
    day_anomalies: int | None = None
    error_type: str | None = None
    error: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "unit_key": self.unit_key, "outcome": self.outcome, "recon_status": self.recon_status,
            "expected": self.expected, "collected": self.collected, "file_gaps": self.file_gaps,
            "no_longer_observed": self.no_longer_observed, "basis": self.basis,
            "day_anomalies": self.day_anomalies, "error_type": self.error_type,
            "error": self.error,
        }  # fmt: skip

    @property
    def leaf(self) -> int:
        return _leaf(self.record())


def unit_fact_from_event(event_type: str, payload: Mapping[str, Any]) -> UnitFact:
    """`unit_reconciled` / `unit_failed` as written by `edisc_worker.pipeline`."""
    key = str(payload["unit_key"])
    if event_type == "unit_reconciled":
        return UnitFact(
            key, "reconciled", str(payload["recon_status"]),
            expected=payload.get("expected"), collected=payload.get("collected"),
            file_gaps=payload.get("file_gaps"),
            no_longer_observed=payload.get("no_longer_observed"),
            basis=payload.get("basis"), day_anomalies=payload.get("day_anomalies"),
        )  # fmt: skip
    if event_type == "unit_failed":
        return UnitFact(
            key, "failed", ReconStatus.FAILED.value,
            error_type=str(payload.get("error_type")), error=str(payload.get("error")),
        )  # fmt: skip
    raise ValueError(f"not a unit event: {event_type}")


def split_error(last_error: str | None) -> tuple[str | None, str | None]:
    """`work_units.last_error` is `"<type>: <error>"[:4000]`; the chain keeps `<error>[:2000]`."""
    if last_error is None:
        return None, None
    kind, sep, rest = last_error.partition(": ")
    return (kind, rest[:2000]) if sep else (None, last_error[:2000])


def unit_fact_from_row(
    row: Mapping[str, Any], *, no_longer_observed: int, archive_backed: bool
) -> UnitFact | None:
    """The database's version of the unit's chain fact; None while the unit is unsettled (no chain
    event is due yet: pending, running, retry_later, paused)."""
    key, status = str(row["unit_key"]), row["status"]
    if status == UnitStatus.FAILED.value:
        error_type, error = split_error(row.get("last_error"))
        return UnitFact(key, "failed", ReconStatus.FAILED.value, error_type=error_type, error=error)
    if status != UnitStatus.DONE.value:
        return None
    archive = archive_backed and row.get("kind") != "directory"
    return UnitFact(
        key, "reconciled", str(row["recon_status"]),
        expected=row.get("expected_count"), collected=row.get("collected_count"),
        file_gaps=row.get("file_gaps"), no_longer_observed=no_longer_observed,
        basis="archive" if archive else None,
        day_anomalies=row.get("day_anomalies") if archive else None,
    )  # fmt: skip


@dataclass
class DigestFold:
    """Order-independent multiset digest of unit facts: sum of SHA-256(canonical record) mod 2^256,
    overall and per bucket (128 KiB). Memory O(1) in the number of units."""

    total: int = 0
    count: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * BUCKETS)

    def add(self, fact: UnitFact) -> None:
        leaf = fact.leaf
        self.total = (self.total + leaf) % _MOD
        b = bucket_of(fact.unit_key)
        self.buckets[b] = (self.buckets[b] + leaf) % _MOD
        self.count += 1

    @property
    def digest(self) -> str:
        return f"{self.total:064x}"

    def differing(self, other: DigestFold) -> list[int]:
        if (self.total, self.count) == (other.total, other.count) and self.buckets == other.buckets:
            return []
        return [i for i in range(BUCKETS) if self.buckets[i] != other.buckets[i]]


@dataclass(frozen=True)
class Divergence:
    """A primary source and its cross-check disagree (§8). The chain value is the fact."""

    kind: str
    subject: str
    chain: Any
    database: Any

    def record(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "chain": self.chain,
                "database": self.database}  # fmt: skip


def compare_units(
    chain: Iterable[UnitFact], database: Iterable[UnitFact]
) -> tuple[list[Divergence], dict[str, UnitFact]]:
    """Unit by unit, for the units of the differing buckets: divergences, and the chain fact to state
    for every unit the chain speaks about (the first event when it holds several)."""
    by_chain: dict[str, list[UnitFact]] = {}
    for f in chain:
        by_chain.setdefault(f.unit_key, []).append(f)
    by_db: dict[str, list[UnitFact]] = {}
    for f in database:
        by_db.setdefault(f.unit_key, []).append(f)
    out: list[Divergence] = []
    stated: dict[str, UnitFact] = {}
    for key in sorted(by_chain.keys() | by_db.keys()):
        cs, ds = by_chain.get(key, []), by_db.get(key, [])
        if cs:
            stated[key] = cs[0]
        if len(cs) > 1:
            out.append(Divergence("duplicate_unit_event", key, [c.record() for c in cs], None))
        if cs and not ds:
            out.append(Divergence("unit_unsettled_in_database", key, cs[0].record(), None))
        elif ds and not cs:
            out.append(Divergence("unit_missing_from_chain", key, None, ds[0].record()))
        elif cs and ds and cs[0] != ds[0]:
            out.append(Divergence("unit_differs", key, cs[0].record(), ds[0].record()))
    return out, stated


# ------------------------------------------------------------------ the job chain
@dataclass(frozen=True)
class ChainEvent:
    seq: int
    event_type: str
    actor: str
    created_at: datetime
    payload: Mapping[str, Any]


@dataclass
class Pause:
    reason: str | None
    connection_id: str | None
    paused_at: datetime
    resumed_at: datetime | None = None
    resumed_by: str | None = None

    def record(self) -> dict[str, Any]:
        duration = (
            None
            if self.resumed_at is None
            else round((self.resumed_at - self.paused_at).total_seconds() * 1000)
        )
        return {
            "reason": self.reason, "connection_id": self.connection_id,
            "paused_at": iso(self.paused_at), "resumed_at": iso(self.resumed_at),
            "duration_ms": duration, "resumed_by": self.resumed_by,
        }  # fmt: skip


@dataclass
class ChainFold:
    """One pass over the VERIFIED job chain in seq order. Memory: O(pauses + distinct actors)."""

    started: ChainEvent | None = None
    final: ChainEvent | None = None
    stops: list[ChainEvent] = field(default_factory=list)  # cancel_requested / job_failed
    pauses: list[Pause] = field(default_factory=list)
    units: DigestFold = field(default_factory=DigestFold)
    outcomes: Counter[str] = field(default_factory=Counter)  # recon status of each unit event
    actors: Counter[tuple[str, str]] = field(default_factory=Counter)
    events: int = 0
    batches: int = 0
    duplicate_starts: int = 0
    duplicate_finals: int = 0

    def add(self, e: ChainEvent) -> UnitFact | None:
        """Fold one event; returns its unit fact for a unit event."""
        self.events += 1
        self.actors[(e.event_type, e.actor)] += 1
        if e.event_type == "job_started":
            if self.started is None:
                self.started = e
            else:
                self.duplicate_starts += 1
        elif e.event_type in FINAL_EVENTS:
            if self.final is None:
                self.final = e
            else:
                self.duplicate_finals += 1
        elif e.event_type in ("cancel_requested", "job_failed"):
            self.stops.append(e)
        elif e.event_type == "job_paused":
            self.pauses.append(
                Pause(e.payload.get("reason"), e.payload.get("connection_id"), e.created_at)
            )
        elif e.event_type == "job_resumed":
            for p in self.pauses:
                if p.resumed_at is None:
                    p.resumed_at, p.resumed_by = e.created_at, e.actor
        elif e.event_type == "items_collected":
            self.batches += 1
        elif e.event_type in UNIT_EVENTS:
            fact = unit_fact_from_event(e.event_type, e.payload)
            self.units.add(fact)
            self.outcomes[fact.recon_status] += 1
            return fact
        return None

    @property
    def final_status(self) -> str | None:
        return None if self.final is None else str(self.final.payload.get("status"))


# ------------------------------------------------------------------ JSONL rows
def scope_refs(started: ChainEvent | None) -> list[dict[str, Any]]:
    return [] if started is None else list(started.payload.get("scopes", []))


def unit_row(
    row: Mapping[str, Any],
    *,
    stated: UnitFact | None,
    zone: str,
    scopes: Sequence[int],
    divergent: bool,
) -> dict[str, Any]:
    """One `units.jsonl` row (§1). ``stated`` is the CHAIN fact when the chain speaks about the unit
    (it then wins over the row), else None and the row is reported as database-sourced. ``scopes``:
    indexes into `job_started.scopes` of the scopes covering the unit."""
    key = str(row["unit_key"])
    base = {
        "unit_key": key,
        "kind": row.get("kind"),
        "conversation_id": row.get("conversation_id") or None,
        "day": day_label(key),
        "zone": zone,
        "scopes": list(scopes),
        "access_lost_reason": row.get("access_lost_reason"),
        "divergent": divergent,
    }
    if stated is not None:
        f = stated
        return {
            **base, "source": "job_chain",
            "status": UnitStatus.FAILED.value if f.outcome == "failed" else UnitStatus.DONE.value,
            "recon_status": f.recon_status, "expected": f.expected, "collected": f.collected,
            "file_gaps": f.file_gaps, "no_longer_observed": f.no_longer_observed,
            "basis": f.basis, "day_anomalies": f.day_anomalies, "error_type": f.error_type,
            "error": f.error,
        }  # fmt: skip
    return {
        **base, "source": "database", "status": row["status"],
        "recon_status": row["recon_status"], "expected": row.get("expected_count"),
        "collected": row.get("collected_count"), "file_gaps": row.get("file_gaps"),
        "no_longer_observed": None, "basis": None, "day_anomalies": None,
        "error_type": None, "error": None,
    }  # fmt: skip


def unit_order(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """`units.jsonl` order: (conversation id, day, unit key)."""
    return (str(row.get("conversation_id") or ""), str(row.get("day") or ""), str(row["unit_key"]))


def unit_severity(row: Mapping[str, Any]) -> str:
    """A `units.jsonl` row's place in the severity order: "failed" for a failed unit, else its
    recon status (§4.6)."""
    return "failed" if row["status"] == UnitStatus.FAILED.value else str(row["recon_status"])


def worst(statuses: Iterable[str]) -> str | None:
    return min(statuses, key=lambda s: (_RANK.get(s, len(SEVERITY)), s), default=None)


class ConversationFold:
    """Per-conversation aggregates over `units.jsonl` rows IN FILE ORDER (conversation id first), so
    a conversation is complete when the next one starts: memory O(1) in the number of units. Units
    with no conversation (the directory unit) are not part of any conversation row.

    A `conversations.jsonl` row (§1): conversation id, units, the worst unit status, units per recon
    status (zeros omitted), expected, collected and file gaps summed (null values count 0)."""

    def __init__(self) -> None:
        self._cur: dict[str, Any] | None = None
        self.count = 0

    def add(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        """Fold one unit row; returns the previous conversation's row when this one starts a new
        conversation."""
        conv = row.get("conversation_id")
        if not conv:
            return None
        done = None
        if self._cur is not None and self._cur["conversation_id"] != conv:
            done = self._close()
        if self._cur is None:
            self._cur = {"conversation_id": conv, "units": 0, "statuses": Counter(),
                         "expected": 0, "collected": 0, "file_gaps": 0}  # fmt: skip
        cur = self._cur
        cur["units"] += 1
        cur["statuses"][unit_severity(row)] += 1
        for k in ("expected", "collected", "file_gaps"):
            cur[k] += int(row.get(k) or 0)
        return done

    def finish(self) -> dict[str, Any] | None:
        return None if self._cur is None else self._close()

    def _close(self) -> dict[str, Any]:
        cur, self._cur = self._cur, None
        if cur is None:
            raise RuntimeError("no open conversation")
        self.count += 1
        statuses: Counter[str] = cur["statuses"]
        return {
            "conversation_id": cur["conversation_id"], "units": cur["units"],
            "worst_status": worst(statuses), "units_by_status": dict(sorted(statuses.items())),
            "expected": cur["expected"], "collected": cur["collected"],
            "file_gaps": cur["file_gaps"],
        }  # fmt: skip


def conversations_section(capped: Capped, file: Mapping[str, Any] | None) -> dict[str, Any]:
    """The per-conversation list in `report.json` (§1, §4.6): every conversation when there are at
    most CAP of them (and no file), else the CAP worst with the rest named by `conversations.jsonl`
    and its SHA-256. Above the cap the file MUST exist: a report never names a missing file."""
    if capped.total > capped.cap and file is None:
        raise ValueError(
            f"{capped.total} conversations need conversations.jsonl (cap {capped.cap})"
        )
    return {"source": "job_chain", **capped.record(file)}


def observation_row(
    *, unit_key: str, item_id: str, kind: str, source_item_id: str, derived: Mapping[str, Any]
) -> dict[str, Any]:
    """One `observations.jsonl` row: an item-level event linked to the job (§1)."""
    return {
        "unit_key": unit_key,
        "item_id": item_id,
        "kind": kind,
        "subject": source_item_id,
        "file_id": derived.get("file_id"),
        "reason": derived.get("reason"),
        "observed_at": derived.get("observed_at"),
    }


def render_row(render: Mapping[str, Any]) -> dict[str, Any]:
    """One `renders.jsonl` row, from the snapshot (§1, §9): the render's identity, status, head and
    seal, files, natives and every external native."""
    keys = (
        "render_id", "status", "renderer_version", "unicode_version", "tzdata_version",
        "options_hash", "head_seq", "head_hash", "seal_key", "seal_version_id", "files",
        "natives", "natives_bytes", "externals",
    )  # fmt: skip
    return {k: render.get(k) for k in keys}


# ------------------------------------------------------------------ capped lists, digests
class _Last:
    """Reverses the order of a sort key, so a min-heap keeps the LARGEST key on top."""

    __slots__ = ("key",)

    def __init__(self, key: tuple[Any, ...]) -> None:
        self.key = key

    def __lt__(self, other: _Last) -> bool:
        return self.key > other.key


class Capped:
    """The CAP worst rows of a stream (§4.6): worst status first, then the stable key; the exact
    total is kept. Memory O(CAP)."""

    def __init__(self, cap: int = CAP) -> None:
        self.cap, self.total = cap, 0
        self._heap: list[tuple[_Last, dict[str, Any]]] = []

    def add(self, status: str, key: tuple[Any, ...], row: dict[str, Any]) -> None:
        self.total += 1
        order = _Last((_RANK.get(status, len(SEVERITY)), *key))
        if len(self._heap) < self.cap:
            heapq.heappush(self._heap, (order, row))
        elif order.key < self._heap[0][0].key:
            heapq.heapreplace(self._heap, (order, row))

    def rows(self) -> list[dict[str, Any]]:
        return [r for _, r in sorted(self._heap, key=lambda x: x[0].key)]

    def record(self, file: Mapping[str, Any] | None) -> dict[str, Any]:
        """The capped list as `report.json` holds it: rows, the exact total, and where the rest is."""
        rest = self.total - len(self._heap)
        return {"rows": self.rows(), "total": self.total, "more": rest,
                "more_in": None if not rest or file is None else
                {"name": file["name"], "sha256": file["sha256"]}}  # fmt: skip


@dataclass
class JsonlDigest:
    """SHA-256, size and row count of a JSONL file as its lines stream by."""

    name: str
    sha: Any = field(default_factory=hashlib.sha256)
    size: int = 0
    rows: int = 0

    def add(self, line: bytes) -> bytes:
        self.sha.update(line)
        self.size += len(line)
        self.rows += 1
        return line

    def record(self) -> dict[str, Any]:
        return {"name": self.name, "sha256": self.sha.hexdigest(), "size": self.size,
                "rows": self.rows}  # fmt: skip


# ------------------------------------------------------------------ clean (§4.1)
def clean(status: str, *, custody_ok: bool, divergences: int, retention_gaps: int) -> bool:
    """THE clean verdict, for every output: completed AND custody verified AND no divergence AND no
    retention gap touching the job's evidence. `matched_against_archive` / archive jobs are never
    clean (they get the archive banner, never the clean mark)."""
    return JobStatus(status).is_clean and custody_ok and divergences == 0 and retention_gaps == 0


def clean_basis(status: str) -> str | None:
    if status == JobStatus.COMPLETED.value:
        return "source"
    if status == JobStatus.COMPLETED_AGAINST_ARCHIVE.value:
        return "archive"
    return None


def banner(status: str, *, custody_ok: bool, divergences: int, units: Mapping[str, int],
           unverifiable: int, is_clean: bool) -> list[str]:  # fmt: skip
    """The banner lines, in words, worst first (§4.2): custody and record problems above all."""
    lines: list[str] = []
    if not custody_ok:
        lines.append("CUSTODY VERIFICATION FAILED")
    if divergences:
        lines.append(
            f"RECORDS DISAGREE: {divergences} divergence(s) between the chain and the database"
        )
    if is_clean:
        lines.append("Complete: every unit reconciled against the source")
    elif status == JobStatus.COMPLETED_AGAINST_ARCHIVE.value:
        lines.append("COMPLETE RELATIVE TO THE PROVIDED EXPORT ONLY")
        lines.append(ARCHIVE_CAVEAT)
    elif status == JobStatus.COMPLETED_UNVERIFIED.value:
        lines.append(
            f"NOT VERIFIED: {unverifiable} units could not be checked against a source count"
        )
    else:
        counts = ", ".join(f"{k} {units.get(k, 0)}" for k in EXCEPTION_RECON if units.get(k, 0))
        lines.append(f"NOT COMPLETE: status {status}" + (f"; {counts}" if counts else ""))
    return lines


# ------------------------------------------------------------------ report.json
def zero_rows(values: Iterable[str], counts: Mapping[str, int]) -> list[dict[str, Any]]:
    """Every enum value in its fixed order, zeros included (§4.3); unknown values after, sorted."""
    order = list(values)
    extra = sorted(k for k in counts if k not in order)
    return [{"value": v, "count": int(counts.get(v, 0))} for v in (*order, *extra)]


def or_unknown(value: Any) -> Any:
    """A fact with no recorded source prints UNKNOWN, never "none" or an empty list (§4.4)."""
    return UNKNOWN if value is None else value


@dataclass(frozen=True)
class ReportInputs:
    """Everything `report.json` states. Built by the loader; every field is already JSON-ready
    except where typed."""

    job: Mapping[str, Any]  # database facts of the job (ids, times, status, rerun_of)
    chain: ChainFold
    verification: Mapping[str, Any]  # VerificationReport.as_dict() + seal key/version
    snapshot: Mapping[str, Any]  # renders, retention gaps, lock settings, audit head (+ digest)
    access: Mapping[str, Any]  # connection row facts, export facts (database)
    unit_status_counts: Mapping[str, int]  # status of every unit as stated (units.jsonl)
    recon_counts: Mapping[str, int]
    totals: Mapping[str, int]  # expected, collected, file_gaps over the stated units
    exceptions: Mapping[str, Any]  # capped units, observation counts by kind and reason, orphans
    observation_counts: Mapping[str, int]
    pauses_db: Sequence[Mapping[str, Any]]
    divergences: Sequence[Divergence]
    normalizer_versions: Sequence[str]
    versions: Mapping[str, Any]  # renderer, unicode, toolchain (None until PDF), image digest
    audit_events: Sequence[Mapping[str, Any]]
    files: Sequence[JsonlDigest]
    evidence: Mapping[str, Any]  # retain-until range of the job's evidence
    identity: Mapping[str, Any]
    conversations: Mapping[str, Any] = field(default_factory=dict)  # `conversations_section`


def report_document(inp: ReportInputs) -> dict[str, Any]:
    """The `report.json` document (§1.1). Sections in a fixed order, each with its source."""
    started = inp.chain.started
    sp = dict(started.payload) if started is not None else {}
    status = inp.chain.final_status or str(inp.job.get("status"))
    custody_ok = bool(inp.verification.get("ok"))
    gaps = len(inp.snapshot.get("retention_gaps", []))
    is_clean = clean(status, custody_ok=custody_ok, divergences=len(inp.divergences),
                     retention_gaps=gaps)  # fmt: skip
    files = {f.name: f.record() for f in inp.files}
    zone = sp.get("unit_day_zone")
    return {
        "format": REPORT_FORMAT,
        "renderer_version": REPORT_RENDERER_VERSION,
        "banner": banner(
            status,
            custody_ok=custody_ok,
            divergences=len(inp.divergences),
            units=inp.recon_counts,
            unverifiable=int(inp.recon_counts.get("unverifiable", 0)),
            is_clean=is_clean,
        ),
        "job": {
            "source": "job_chain",
            **dict(inp.job),
            "requested_by": started.actor if started is not None else UNKNOWN,
            "status": status,
            "status_database": inp.job.get("status"),
            "clean": is_clean,
            "clean_basis": clean_basis(status),
            "archive_caveat": ARCHIVE_CAVEAT
            if status == JobStatus.COMPLETED_AGAINST_ARCHIVE.value
            else None,
            "unit_day_zone": zone if zone is not None else ZONE_NOT_RECORDED,
            "unit_day_zone_source": "job_chain" if zone is not None else "not_recorded",
        },
        "scopes": {"source": "job_chain", "scopes": scope_refs(started)},
        "access": {
            "source": "job_chain" if "plan_tier" in sp else "not_recorded",
            "connector": or_unknown(sp.get("connector")),
            "connector_version": or_unknown(sp.get("connector_version")),
            "connection_id": or_unknown(sp.get("connection_id")),
            "plan_tier": or_unknown(sp.get("plan_tier")),
            "granted_scopes": or_unknown(sp.get("granted_scopes")),
            "blind_spots": or_unknown(sp.get("blind_spots")),
            "export": sp.get("export"),
            "database": dict(inp.access),
        },
        "exceptions": {
            "source": "job_chain",
            **dict(inp.exceptions),
            "observations": zero_rows(OBSERVATION_KINDS, inp.observation_counts),
        },
        "counts": {
            "source": "job_chain",
            "units_by_status": zero_rows((s.value for s in UnitStatus), inp.unit_status_counts),
            "units_by_recon_status": zero_rows((s.value for s in ReconStatus), inp.recon_counts),
            **{k: int(v) for k, v in sorted(inp.totals.items())},
        },
        "conversations": dict(inp.conversations)
        or {"source": "job_chain", "rows": [], "total": 0, "more": 0, "more_in": None},
        "pauses": {
            "source": "job_chain",
            "pauses": [p.record() for p in inp.chain.pauses],
            "never_resumed": sum(1 for p in inp.chain.pauses if p.resumed_at is None),
        },
        "versions": {
            "source": "job_chain",
            "connector_version": or_unknown(sp.get("connector_version")),
            "normalizer_versions": list(inp.normalizer_versions) or UNKNOWN,
            **dict(inp.versions),
        },
        "actors": {
            "source": "job_chain",
            "custody": [
                {"event_type": t, "actor": a, "events": n}
                for (t, a), n in sorted(inp.chain.actors.items())
            ],
            "audit": list(inp.audit_events),
            "audit_source": "snapshot",
        },
        "custody_verification": {"source": "job_chain", **dict(inp.verification)},
        "evidence_store": {
            "source": "snapshot",
            "lock": inp.snapshot.get("lock"),
            "retention_gaps": list(inp.snapshot.get("retention_gaps", [])),
            **dict(inp.evidence),
        },
        "renders": {
            "source": "snapshot",
            "renders": len(inp.snapshot.get("renders", [])),
            "file": files.get("renders.jsonl"),
        },
        "divergences": {
            "source": "job_chain",
            "divergences": [d.record() for d in inp.divergences],
        },
        "integrity": {
            "source": "snapshot",
            "snapshot_digest": inp.snapshot.get("digest"),
            "files": [files[k] for k in sorted(files)],
            "identity": dict(inp.identity),
        },
    }


def report_json(inp: ReportInputs) -> bytes:
    return canonical_json(report_document(inp))
