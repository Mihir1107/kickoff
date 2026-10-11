"""Offline verification of report packages (``edisc-report-package/1``, ADR 0018 §14, §27). No
database, network or credentials (a package directory or the downloaded zip, read in place).

Checked: the manifest against every JSONL file; nothing in the package the manifest does not account
for; every object's SHA-256 and size; the report's chain (hashes, links, anchors as listed from the
bucket, its seal when it is final); that it starts with ``report_started`` (or ``report_refused``)
for this report, whose reference to the job (id, final head, seal key and version) is the included
seal anchor, which anchors exactly that head; ``report_generated``'s file records: their RFC 6962
root, the manifest's outputs, and every ``outputs/<name>``'s SHA-256 and size; and that `report.json`
names the same SHA-256 and size for each JSONL file. With the job's custody package
(``--job-package``): the job package verifies, its head and seal are the referenced ones, and the
report's chain-derived content is RECOMPUTED from the job's events with the report model
(``edisc_renderers.report.recompute``) and byte-compared; what cannot be recomputed offline (the
database-sourced columns, observations, renders, the snapshot) is checked against its recorded
digests only, and the report says so (``not_recomputed``).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from edisc_custody.archive import ArchiveError
from edisc_custody.chain import Anchor, ChainVerifier, EventRecord, VerificationReport
from edisc_custody.package import PackageFormatError, PackageReport, verify_package
from edisc_custody.package_source import (
    PackageSource,
    ZipSource,
    lines,
    open_source,
    read_all,
)
from edisc_custody.render_package import check_job_seal, is_os_metadata
from edisc_custody.report_files import (
    REPORT_GENERATED,
    REPORT_REFUSED,
    REPORT_STARTED,
    files_root,
)

REPORT_PACKAGE_FORMAT = "edisc-report-package/1"
REPORT_FILES = ("events.jsonl", "anchors.jsonl", "job_seal.json")
MAX_OBJECT_BYTES = 1 << 20  # anchors and seals are small JSON documents
NOT_RECOMPUTED = (
    "units.jsonl database columns (kind, scopes, access-lost reason)",
    "observations.jsonl", "renders.jsonl", "conversations.jsonl",
    "the snapshot (renders, retention gaps, lock settings, audit events) and the S3 observation",
)  # fmt: skip
FINAL = ("completed", "failed", "refused")


@dataclass
class ReportPackageReport:
    chain: VerificationReport | None = None
    job: PackageReport | None = None
    manifest_sha256: str = ""
    outputs_checked: int = 0
    recomputed: bool = False  # the chain-derived content was recomputed and matched
    not_recomputed: tuple[str, ...] = ()
    tolerated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.errors
            and self.chain is not None
            and self.chain.ok
            and (self.job is None or self.job.ok)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "manifest_sha256": self.manifest_sha256,
            "outputs_checked": self.outputs_checked,
            "recomputed": self.recomputed,
            "not_recomputed": list(self.not_recomputed),
            "errors": self.errors,
            "tolerated": self.tolerated,
            "chain": self.chain.as_dict() if self.chain else None,
            "job": self.job.as_dict() if self.job else None,
        }


def is_report_package(path: Path) -> bool:
    try:
        source = open_source(path)
        try:
            manifest = json.loads(read_all(source, "manifest.json"))
        finally:
            if isinstance(source, ZipSource):
                source.close()
    except (OSError, ValueError, ArchiveError, KeyError):
        return False
    return isinstance(manifest, dict) and manifest.get("format") == REPORT_PACKAGE_FORMAT


def _lines(source: PackageSource, name: str) -> Iterator[dict[str, Any]]:
    for n, raw in enumerate(lines(source, name), start=1):
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise PackageFormatError(f"{name}:{n}: invalid JSON") from exc
        if not isinstance(obj, dict):
            raise PackageFormatError(f"{name}:{n}: expected an object")
        yield obj


def verify_report_package(
    root: Path, job_package: Path | None = None, *, tolerate_os_metadata: bool = False
) -> ReportPackageReport:
    """``root``: the package directory or zip. ``job_package``: the job's custody package
    (directory), verified, matched, and used to recompute the chain-derived content."""
    source = open_source(root)
    tolerate = tolerate_os_metadata and not isinstance(source, ZipSource)
    try:
        return _verify(source, job_package, tolerate)
    finally:
        if isinstance(source, ZipSource):
            source.close()


def _verify(source: PackageSource, job_package: Path | None, tolerate: bool) -> ReportPackageReport:
    report = ReportPackageReport()
    manifest_bytes = read_all(source, "manifest.json")
    report.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != REPORT_PACKAGE_FORMAT:
        raise PackageFormatError(f"unsupported report package format {manifest.get('format')!r}")

    # 1. every JSONL file is exactly what the manifest says
    for name in REPORT_FILES:
        expected = manifest["files"].get(name)
        if expected is None or not source.exists(name):
            report.errors.append(f"{name}: missing from package or manifest")
            continue
        digest, count, size = hashlib.sha256(), 0, 0
        for raw in lines(source, name):
            digest.update(raw)
            count += 1
            size += len(raw)
        if (digest.hexdigest(), count, size) != (
            expected["sha256"],
            expected["lines"],
            expected.get("bytes"),
        ):
            report.errors.append(f"{name}: does not match manifest (modified after export)")
    if report.errors:
        return report

    tenant_id, report_id, job_id = manifest["tenant_id"], manifest["report_id"], manifest["job_id"]
    verifier = ChainVerifier(tenant_id, report_id)
    objects: set[str] = set()

    def body(rec: dict[str, Any], what: str) -> bytes | None:
        sha, size = rec.get("sha256"), rec.get("size")
        name = f"objects/{sha}"
        if not isinstance(sha, str) or not isinstance(size, int) or not source.exists(name):
            report.errors.append(f"{what}: its object {name} is not in the package")
            return None
        objects.add(name)
        try:
            data = read_all(source, name, MAX_OBJECT_BYTES)
        except (ValueError, ArchiveError) as exc:
            report.errors.append(f"{name}: cannot be read ({exc})")
            return None
        if (hashlib.sha256(data).hexdigest(), len(data)) != (sha, size):
            report.errors.append(f"{name}: bytes do not match the recorded sha256/size")
            return None
        return data

    # 2. anchors first, then the report's events in order
    for rec in _lines(source, "anchors.jsonl"):
        if rec.get("delete_marker"):
            verifier.add_hidden_anchor(rec["key"], rec["version_id"])
            continue
        data = body(rec, f"anchor {rec.get('key')} (version {rec.get('version_id')})")
        if data is not None:
            verifier.add_anchor(Anchor(rec["key"], rec["version_id"], data))
    first: EventRecord | None = None
    generated: dict[str, Any] | None = None
    for rec in _lines(source, "events.jsonl"):
        ev = EventRecord(rec["id"], rec["fields"], rec["prev_hash"], rec["event_hash"])
        first = first or ev
        if ev.event_type == REPORT_GENERATED:
            generated = dict(ev.fields.get("payload", {}))
        verifier.add_event(ev)
    head = manifest.get("head")
    report.chain = verifier.finish(
        require_seal=manifest.get("status") in FINAL,
        expected_head=(head["seq"], head["hash"]) if head else None,
    )

    # 3. the stream is this report's, and its reference to the job is the included seal
    reference = _check_start(report, first, report_id, job_id)
    seal = json.loads(read_all(source, "job_seal.json"))
    seal_body = body(seal, "job_seal.json") if seal.get("key") is not None else None
    if reference is not None:
        check_job_seal(report.errors, tenant_id, job_id, reference, seal, seal_body,
                       "report_started")  # fmt: skip

    # 4. the files: report_generated's records, their root, the manifest, the bytes
    records = list(generated.get("files", [])) if generated else []
    if generated is not None and files_root(records) != generated.get("files_root"):
        report.errors.append("report_generated: files_root does not match its file records")
    if manifest.get("status") == "completed" and generated is None:
        report.errors.append("a completed report has no report_generated event")
    listed = [{"name": r.get("name"), "sha256": r.get("sha256"), "size": r.get("size")}
              for r in records]  # fmt: skip
    if listed != manifest.get("outputs", []):
        report.errors.append("manifest outputs differ from report_generated's file records")
    outputs: dict[str, bytes] = {}
    for r in records:
        name = f"outputs/{r.get('name')}"
        if not source.exists(name):
            report.errors.append(f"{name}: missing")
            continue
        try:
            data = read_all(source, name, 1 << 30)
        except (ValueError, ArchiveError) as exc:
            report.errors.append(f"{name}: cannot be read ({exc})")
            continue
        if (hashlib.sha256(data).hexdigest(), len(data)) != (r.get("sha256"), r.get("size")):
            report.errors.append(f"{name}: bytes do not match the recorded sha256/size")
            continue
        report.outputs_checked += 1
        outputs[str(r.get("name"))] = data
    _check_report_json(report, outputs)
    allowed = {
        "manifest.json",
        *REPORT_FILES,
        *objects,
        *(f"outputs/{r.get('name')}" for r in records),
    }
    for extra in sorted(set(source.names()) - allowed):
        if tolerate and is_os_metadata(extra):
            report.tolerated.append(extra)
        else:
            report.errors.append(f"{extra}: not part of this package")

    # 5. optionally, the job itself, and the chain-derived content recomputed from it
    if job_package is not None:
        _with_job(report, job_package, tenant_id, job_id, reference, seal, seal_body, outputs)
    return report


def _check_start(
    report: ReportPackageReport, first: EventRecord | None, report_id: str, job_id: str
) -> dict[str, Any] | None:
    if first is None:
        report.errors.append("events.jsonl: the report stream is empty")
        return None
    payload = first.fields.get("payload", {})
    if first.event_type not in (REPORT_STARTED, REPORT_REFUSED):
        report.errors.append(
            f"the report stream starts with {first.event_type}, not report_started"
        )
        return None
    if payload.get("report_id") != report_id or first.fields.get("job_id") is not None:
        report.errors.append("the first event belongs to another report (or names a job stream)")
    if first.event_type == REPORT_REFUSED:
        return None
    reference = payload.get("job")
    if not isinstance(reference, dict) or reference.get("id") != job_id:
        report.errors.append("report_started does not reference this package's job")
        return None
    return reference


def _check_report_json(report: ReportPackageReport, outputs: dict[str, bytes]) -> None:
    """`report.json` names each JSONL file it summarises by SHA-256 and size: they must be these."""
    if "report.json" not in outputs:
        return
    try:
        doc = json.loads(outputs["report.json"])
        named = {f["name"]: f for f in doc["integrity"]["files"]}
    except (ValueError, KeyError, TypeError):
        report.errors.append("report.json: not a collection report")
        return
    for name, rec in sorted(named.items()):
        data = outputs.get(name)
        if data is None:
            report.errors.append(f"report.json names {name}, which the report does not hold")
        elif (hashlib.sha256(data).hexdigest(), len(data)) != (rec.get("sha256"), rec.get("size")):
            report.errors.append(f"report.json: {name} is not the file it names")


def _with_job(
    report: ReportPackageReport, job_package: Path, tenant_id: str, job_id: str,
    reference: dict[str, Any] | None, seal: dict[str, Any], seal_body: bytes | None,
    outputs: dict[str, bytes],
) -> None:  # fmt: skip
    report.job = verify_package(job_package)
    jm = json.loads((job_package / "manifest.json").read_bytes())
    if jm.get("stream_id") != job_id or jm.get("tenant_id") != tenant_id:
        report.errors.append("job package: belongs to another job or tenant")
        return
    if reference is not None:
        ref_head = reference.get("head", {})
        if jm.get("head") != {"seq": ref_head.get("seq"), "hash": ref_head.get("hash")}:
            report.errors.append("job package: its head is not the head the report references")
        anchors = {
            (a["key"], a["version_id"]): a.get("body_b64")
            for a in _lines(open_source(job_package), "anchors.jsonl")
        }
        held = anchors.get((seal.get("key"), seal.get("version_id")))
        if seal_body is None or held is None or base64.b64decode(held) != seal_body:
            report.errors.append("job package: does not hold the referenced seal anchor")
    if not report.job.ok:
        return  # never recompute from a chain that does not verify
    if "report.json" not in outputs or "units.jsonl" not in outputs:
        report.errors.append("recomputation: the report holds no report.json/units.jsonl")
        return
    try:
        from edisc_renderers.report.recompute import chain_projection, compare, report_projection
    except ImportError as exc:  # never a silent skip: the check was asked for
        report.errors.append(f"recomputation needs edisc-renderers: {exc}")
        return
    chain = chain_projection(_lines(open_source(job_package), "events.jsonl"))
    stated = report_projection(outputs["report.json"], outputs["units.jsonl"].splitlines())
    differences = compare(chain, stated)
    report.errors += [f"recomputed from the job chain: {d}" for d in differences]
    report.recomputed = not differences
    report.not_recomputed = NOT_RECOMPUTED
