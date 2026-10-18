"""Offline recomputation of a report from its job's custody chain (ADR 0018 §14, §27, §28). PURE (no
DB, S3 or clock: the purity test covers it); `edisc-verify --job-package` calls it.

The report states facts from the verified job chain first (§7). Since §28 every unit event of a job
carries the unit's database-only facts (kind, conversation, scopes, access-lost reason, gap reasons)
and the units left unsettled at the end are recorded too (`units_unsettled`), so for such a job the
unit rows come from the chain alone. ``recompute`` folds the job package's events with the report's
own model (`ChainFold`, `unit_row`, `jsonl_line`) and rebuilds, then byte-compares (RFC 8785):

- ``units.jsonl``, byte for byte: every unit's row from its chain fact, in the file order
  (conversation, day, unit key: code points), the zone from `job_started`, the divergent flag from
  the report's own divergence list (a comparison with the database, which only the database can
  redo). A job recorded before §28 states UNKNOWN for the scopes, access-lost reason and gap reasons,
  and the kind and conversation from the unit key: those rows recompute too. A row the report states
  from the DATABASE (a unit the chain never mentions: an unsettled unit of a job recorded before §28,
  or a database row missing from the chain, which the report lists as a divergence) cannot be: it is
  taken as written, counted and named in the output;
- from those rows, exactly as the loader derives them: the unit counts by status and recon status,
  the totals, the capped unit exceptions, the conversations section and ``conversations.jsonl``;
- ``job``: who started it, the final status (when the chain has a final event), the unit day zone;
  ``scopes``, ``access`` as `job_started` recorded them; ``pauses``: each pause's reason class,
  deadline and how it ended (re-authorized, the deadline, finished without access: when, by whom,
  the reason given) and the counts by ending (reports before renderer 1.6.0: who re-authorized and
  the count never resumed); ``actors``: every custody event's actor by type; ``versions``: the normalizer
  versions `job_started` and each batch recorded (jobs started since ADR 0018 §30; older jobs: not
  recomputed);
- the clean VERDICT from the chain alone (`chain_verdict`, §30): the report may never state clean
  when the chain does not give clean (a difference: the package FAILS). The other way round is
  allowed, because the report's verdict also needs what only the database and the bucket know (no
  divergence, no retention gap): the package passes and the reasons the report states are listed
  (``Recomputation.stated_not_clean``).

Not recomputable offline (checked against the digests recorded in `report.json` and
`report_generated` only; ``NOT_RECOMPUTED``): see the constant.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from edisc_core.canonical import canonical_json
from edisc_core.schemas import JobStatus, ReconStatus, UnitStatus
from edisc_renderers.report.model import (
    CLEAN_BANNER,
    EXCEPTION_RECON,
    UNKNOWN,
    ZONE_NOT_RECORDED,
    Capped,
    ChainEvent,
    ChainFold,
    ConversationFold,
    JsonlDigest,
    UnitFact,
    conversations_section,
    exact_document,
    export_section,
    jsonl_line,
    kind_of,
    legacy_pauses_section,
    or_unknown,
    pauses_section,
    unit_order,
    unit_row,
    unit_severity,
    zero_rows,
)

SECTIONS = ("job", "scopes", "access", "pauses", "actors", "versions", "counts",
            "exceptions.units", "conversations")  # fmt: skip
UNIT_DIVERGENCES = (
    "duplicate_unit_event", "unit_unsettled_in_database", "unit_missing_from_chain", "unit_differs",
)  # fmt: skip
NOT_RECOMPUTED = (
    "observations.jsonl and the observation counts (item-level events: the items' derived records,"
    " which the custody chain covers by content hash but does not state)",
    "renders.jsonl and the renders section (each render's own stream, captured in the snapshot)",
    "the snapshot: retention gaps, evidence lock settings and the S3 observation, the job's evidence"
    " statistics, the tenant audit events",
    "the divergences and the divergent flag of a unit row (a comparison with the database)",
    "the banner (it depends on the divergences and retention gaps); the clean verdict is checked one"
    " way only: a report stating clean must be clean by the chain",
    "the report identity, renderer versions and the integrity section (the files' digests are"
    " checked)",
)
NORMALIZERS_NOT_RECORDED = (
    "the normalizer versions (the job was started before job_started recorded them: they come from"
    " the items' derivations)"
)
# the recon statuses that make a conversation-day unit unclean: exactly the pipeline's rule for the
# final status (`Pipeline.finalize`), so a clean job is never contradicted
UNCLEAN_RECON = ("gap", "surplus", "access_lost", "unverifiable", "failed")


def _when(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass
class Recomputation:
    differences: list[str] = field(default_factory=list)
    database_rows: int = 0  # unit rows the report states from the database (taken as written)
    units_rows: int = 0
    conversations_file: bool = False  # conversations.jsonl recomputed (present above the cap)
    chain_clean: bool = False  # the clean verdict from the chain alone
    chain_not_clean: list[str] = field(default_factory=list)  # why the chain does not give clean
    stated_clean: bool = False
    # the report states not clean although the chain gives clean: the reasons it states (allowed)
    stated_not_clean: list[str] = field(default_factory=list)
    normalizers_recorded: bool = False  # `job_started` recorded the normalizer versions

    @property
    def ok(self) -> bool:
        return not self.differences

    def not_recomputed(self) -> tuple[str, ...]:
        extra = (
            (
                f"{self.database_rows} units.jsonl row(s) the report states from the database (the"
                " chain does not mention the unit), taken as written",
            )
            if self.database_rows
            else ()
        )
        older = () if self.normalizers_recorded else (NORMALIZERS_NOT_RECORDED,)
        return extra + NOT_RECOMPUTED + older


def chain_verdict(fold: ChainFold, facts: Mapping[str, UnitFact]) -> list[str]:
    """Why the VERIFIED job chain alone does not give a clean job (empty: it does). Clean by the
    chain: one `job_started`, one final event, `job_finished` with status `completed` (an archive
    job never is, ADR 0014), no stop recorded, and every conversation-day unit the chain states
    settled, reconciled and none of `UNCLEAN_RECON` (the pipeline's own rule for the status, so a
    chain contradicting its final status is not clean either). Custody is verified before this
    runs (never from a package that fails)."""
    out: list[str] = []
    if fold.started is None or fold.duplicate_starts:
        out.append("the chain does not hold exactly one job_started")
    if fold.final is None or fold.duplicate_finals:
        out.append("the chain does not hold exactly one final event")
    elif fold.final.event_type != "job_finished" or fold.final_status != JobStatus.COMPLETED.value:
        out.append(f"final status {fold.final_status} ({fold.final.event_type})")
    out += [f"{stop.event_type} recorded (seq {stop.seq})" for stop in fold.stops]
    unclean: Counter[str] = Counter()
    for key, fact in facts.items():
        kind = fact.detail.kind if fact.detail is not None else kind_of(key)
        if kind != "conversation_day":
            continue
        if fact.outcome != "reconciled":
            unclean[fact.outcome] += 1
        elif fact.recon_status in UNCLEAN_RECON:
            unclean[fact.recon_status] += 1
    out += [f"{n} unit(s) {what}" for what, n in sorted(unclean.items())]
    return out


def stated_clean(doc: Mapping[str, Any]) -> bool:
    """The report states clean when `job.clean` says so OR its banner carries the clean line (the
    HTML and PDF print the banner)."""
    return doc.get("job", {}).get("clean") is True or CLEAN_BANNER in (doc.get("banner") or [])


def stated_reasons(doc: Mapping[str, Any]) -> list[str]:
    """The reasons a report states for not being clean, in words (divergences, retention gaps, a
    custody verification failure, the status)."""
    out: list[str] = []
    job = doc.get("job", {})
    if job.get("status") != JobStatus.COMPLETED.value:
        out.append(f"status {job.get('status')}")
    if doc.get("custody_verification", {}).get("ok") is not True:
        out.append("custody verification not ok when the report was made")
    divergences = doc.get("divergences", {}).get("divergences", [])
    if divergences:
        kinds = Counter(str(d.get("kind")) for d in divergences)
        out.append(
            f"{len(divergences)} divergence(s) between the chain and the database: "
            + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
        )
    gaps = doc.get("evidence_store", {}).get("retention_gaps", [])
    if gaps:
        out.append(f"{len(gaps)} retention gap(s) touching the job's evidence")
    return out or ["none stated"]


def fold_chain(events: Iterable[Mapping[str, Any]]) -> tuple[ChainFold, dict[str, UnitFact]]:
    """The job package's ``events.jsonl`` records (``{"fields": ...}``) in seq order: the fold, and
    the fact the report states for each unit (the first event when the chain holds several)."""
    fold = ChainFold()
    first: dict[str, UnitFact] = {}
    for rec in events:
        f = rec["fields"]
        event = ChainEvent(
            int(f["seq"]), str(f["event_type"]), str(f["actor"]), _when(f["created_at"]),
            dict(f.get("payload") or {}),
        )  # fmt: skip
        for fact in fold.add(event):
            first.setdefault(fact.unit_key, fact)
    return fold, first


def _access(sp: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "connector": or_unknown(sp.get("connector")),
        "connector_version": or_unknown(sp.get("connector_version")),
        "connection_id": or_unknown(sp.get("connection_id")),
        "plan_tier": or_unknown(sp.get("plan_tier")),
        "granted_scopes": or_unknown(sp.get("granted_scopes")),
        "blind_spots": or_unknown(sp.get("blind_spots")),
        "export": export_section(sp.get("export")),
    }


def _legacy_pauses(doc: Mapping[str, Any]) -> bool:
    """A report written before renderer 1.6.0 states pauses in the older shape."""
    try:
        version = tuple(int(x) for x in str(doc.get("renderer_version")).split("."))
    except ValueError:
        return False  # not a version we know: compared in the current shape (and differs)
    return version < (1, 6, 0)


def _job_sections(fold: ChainFold, *, legacy_pauses: bool = False) -> dict[str, Any]:
    started = fold.started
    sp = dict(started.payload) if started is not None else {}
    return {
        "job": {
            "requested_by": started.actor if started is not None else UNKNOWN,
            "final_status": fold.final_status,
            "unit_day_zone": sp.get("unit_day_zone"),
        },
        "scopes": list(sp.get("scopes", [])),
        "access": _access(sp),
        "pauses": (legacy_pauses_section if legacy_pauses else pauses_section)(fold.pauses),
        "actors": [
            {"event_type": t, "actor": a, "events": n} for (t, a), n in sorted(fold.actors.items())
        ],
        "versions": _versions(fold),
    }


def _versions(fold: ChainFold) -> list[str] | None:
    """The normalizer versions the chain records (`job_started` + each batch, §30), or None for a
    job started before they were recorded (not recomputable: the report read the derivations)."""
    versions = fold.normalizer_versions
    return None if versions is None else versions or [UNKNOWN]


def _stated_job_sections(doc: Mapping[str, Any], *, recorded: bool) -> dict[str, Any]:
    job = doc.get("job", {})
    missing_final = any(
        d.get("kind") == "job_final_event_missing"
        for d in doc.get("divergences", {}).get("divergences", [])
    )
    access = doc.get("access", {})
    pauses = doc.get("pauses", {})
    versions = doc.get("versions", {})
    return {
        "job": {
            "requested_by": job.get("requested_by"),
            "final_status": None if missing_final else job.get("status"),
            "unit_day_zone": job.get("unit_day_zone")
            if job.get("unit_day_zone_source") == "job_chain"
            else None,
        },
        "scopes": list(doc.get("scopes", {}).get("scopes", [])),
        "access": {k: access.get(k) for k in _access({})},
        "pauses": {k: v for k, v in pauses.items() if k != "source"},
        "actors": list(doc.get("actors", {}).get("custody", [])),
        "versions": _stated_versions(versions, recorded),
    }


def _stated_versions(versions: Mapping[str, Any], recorded: bool) -> list[str] | None:
    if not recorded:
        return None
    stated = versions.get("normalizer_versions")
    return list(stated) if isinstance(stated, list) else [str(stated)]


def _rows_sections(rows: Sequence[Mapping[str, Any]], units_file: Mapping[str, Any]) -> tuple[
    dict[str, Any], bytes | None
]:  # fmt: skip
    """What the loader derives from the `units.jsonl` rows (in file order): counts, the capped
    exceptions, the conversations section and, above the cap, `conversations.jsonl`'s bytes."""
    status_counts: Counter[str] = Counter()
    recon_counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    exceptions = Capped()
    conversations = ConversationFold()
    capped = Capped()
    conv_rows: list[dict[str, Any]] = []

    def take(conv: dict[str, Any] | None) -> None:
        if conv is not None:
            capped.add(conv["worst_status"], (conv["conversation_id"],), conv)
            conv_rows.append(conv)

    for row in rows:
        status_counts[row["status"]] += 1
        if row["kind"] != "directory":
            recon_counts[row["recon_status"]] += 1
        for k in ("expected", "collected", "file_gaps"):
            totals[k] += int(row[k] or 0)
        severity = unit_severity(row)
        if severity in EXCEPTION_RECON:
            exceptions.add(severity, unit_order(row), dict(row))
        take(conversations.add(row))
    take(conversations.finish())
    conv_file: bytes | None = None
    file_record: dict[str, Any] | None = None
    if conversations.count > capped.cap:
        digest = JsonlDigest("conversations.jsonl")
        conv_file = b"".join(digest.row(c) for c in conv_rows)
        file_record = digest.record()
    sections = {
        "counts": {
            "source": "job_chain",
            "units_by_status": zero_rows((s.value for s in UnitStatus), status_counts),
            "units_by_recon_status": zero_rows((s.value for s in ReconStatus), recon_counts),
            **{k: int(v) for k, v in sorted(totals.items())},
        },
        "exceptions.units": exceptions.record(units_file),
        "conversations": conversations_section(capped, file_record),
    }
    return sections, conv_file


def recompute(
    events: Iterable[Mapping[str, Any]],
    report_json: bytes,
    units_jsonl: bytes,
    conversations_jsonl: bytes | None = None,
) -> Recomputation:
    """Rebuild the report's chain-derived content from the job's events and compare it with what the
    report states (``report_json``, ``units_jsonl`` and, when the report has one,
    ``conversations_jsonl``)."""
    out = Recomputation()
    fold, facts = fold_chain(events)
    doc = exact_document(json.loads(report_json))
    zone_recorded = fold.started.payload.get("unit_day_zone") if fold.started else None
    zone = zone_recorded or ZONE_NOT_RECORDED
    divergent = {
        str(d.get("subject"))
        for d in doc.get("divergences", {}).get("divergences", [])
        if d.get("kind") in UNIT_DIVERGENCES
    }

    # units.jsonl: the chain's rows, plus the rows the report states from the database
    written = [json.loads(line) for line in units_jsonl.splitlines(keepends=True) if line.strip()]
    from_database = [r for r in written if r.get("source") == "database"]
    out.database_rows = len(from_database)
    database_keys = {str(r["unit_key"]) for r in from_database}
    rows = [
        unit_row({"unit_key": key}, stated=fact, zone=zone, scopes=(), divergent=key in divergent)
        for key, fact in facts.items()
        if key not in database_keys
    ]
    rows = sorted([*rows, *from_database], key=unit_order)
    rebuilt = b"".join(jsonl_line(r) for r in rows)
    out.units_rows = len(rows)
    if rebuilt != units_jsonl:
        out.differences.extend(_line_differences("units.jsonl", rebuilt, units_jsonl))
    units_file = {"name": "units.jsonl", "sha256": hashlib.sha256(units_jsonl).hexdigest()}

    # what the report derives from the rows, and the chain's job sections
    derived, conv_file = _rows_sections(rows, units_file)
    mine = {**_job_sections(fold, legacy_pauses=_legacy_pauses(doc)), **derived}
    stated = {
        **_stated_job_sections(doc, recorded=fold.normalizer_versions is not None),
        "counts": doc.get("counts"),
        "exceptions.units": doc.get("exceptions", {}).get("units"),
        "conversations": doc.get("conversations"),
    }
    for section in SECTIONS:
        if canonical_json(mine[section]) != canonical_json(stated[section]):
            out.differences.append(
                f"{section}: the report states {_short(stated[section])}, the chain gives"
                f" {_short(mine[section])}"
            )
    out.normalizers_recorded = fold.normalizer_versions is not None

    # the clean verdict: never clean in the report when the chain does not give clean
    out.chain_not_clean = chain_verdict(fold, facts)
    out.chain_clean = not out.chain_not_clean
    out.stated_clean = stated_clean(doc)
    if out.stated_clean and not out.chain_clean:
        out.differences.append(
            "verdict: the report states clean, the chain does not give clean: "
            + "; ".join(out.chain_not_clean)
        )
    elif out.chain_clean and not out.stated_clean:
        out.stated_not_clean = stated_reasons(doc)
    if conv_file is not None or conversations_jsonl is not None:
        out.conversations_file = True
        if conv_file != conversations_jsonl:
            out.differences.extend(
                _line_differences(
                    "conversations.jsonl", conv_file or b"", conversations_jsonl or b""
                )
            )
    return out


def _line_differences(name: str, rebuilt: bytes, written: bytes) -> list[str]:
    mine, theirs = rebuilt.splitlines(), written.splitlines()
    out = [
        f"{name}: the file is not what the chain gives ({len(theirs)} rows written,"
        f" {len(mine)} recomputed)"
    ]
    shown = 0
    for i in range(max(len(mine), len(theirs))):
        a = mine[i] if i < len(mine) else None
        b = theirs[i] if i < len(theirs) else None
        if a != b:
            out.append(f"{name} row {i + 1}: {_row_difference(_parse(b), _parse(a))}")
            shown += 1
            if shown == 5:
                break
    return out


def _row_difference(written: Any, recomputed: Any) -> str:
    """The fields that differ (written vs recomputed), or both rows when either is missing."""
    if not isinstance(written, dict) or not isinstance(recomputed, dict):
        return f"written {_short(written)}, recomputed {_short(recomputed)}"
    keys = sorted(
        k for k in written.keys() | recomputed.keys() if written.get(k) != recomputed.get(k)
    )
    subject = written.get("unit_key") or written.get("conversation_id")
    fields = "; ".join(
        f"{k}: written {_short(written.get(k))}, recomputed {_short(recomputed.get(k))}"
        for k in keys
    )
    return f"{_short(subject)}: {fields}"


def _parse(line: bytes | None) -> Any:
    return None if line is None else json.loads(line)


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    return text if len(text) <= 300 else text[:297] + "..."
