"""Standalone verification of a render package (ADR 0015 §14, ADR 0008). No database, network or
credentials: stdlib and pure ``edisc_core`` / ``edisc_custody`` modules only.

Format ``edisc-render-package/1``, a directory:

- ``manifest.json``: tenant, render and job ids, whether the render is finalized, its head, and the
  SHA-256 and line count of every file below;
- ``events.jsonl``: the render's custody stream; ``anchors.jsonl``: its WORM anchors, as listed from
  the bucket's object versions;
- ``files.jsonl``: every output file record, in render order, with the ``render_files_batch`` event it
  belongs to;
- ``job_seal.json``: the sealed job's seal anchor (key, VersionId, body) as read from WORM;
- ``outputs/<name>``: the output files themselves, unless the package references them by hash only
  (``outputs_included`` false; the expert supplies them with ``--file``).

Checked: the manifest against every file; the render chain (hashes, links, anchors, its seal) with
every batch's Merkle root, count and position recomputed from its file records and
``render_completed``'s totals and root over the batch roots; that the stream starts with
``render_started`` (or ``render_refused``) for this render, whose reference to the job (id, final head,
seal key and version) equals the included seal anchor, which anchors exactly that head; every output
file's SHA-256 and size, with nothing unlisted in ``outputs/``. With the job's custody package
(``--job-package``) the job chain is verified too, and its head and seal must be the referenced ones.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from edisc_custody.chain import (
    ANCHOR_FORMAT,
    Anchor,
    ChainVerifier,
    EventRecord,
    VerificationReport,
    anchor_key,
)
from edisc_custody.package import PackageFormatError, PackageReport, verify_package
from edisc_custody.render_files import RENDER_BATCH_EVENT, RENDER_REFUSED, RENDER_STARTED

RENDER_PACKAGE_FORMAT = "edisc-render-package/1"
RENDER_FILES = ("events.jsonl", "anchors.jsonl", "files.jsonl", "job_seal.json")


@dataclass
class RenderPackageReport:
    chain: VerificationReport | None = None
    job: PackageReport | None = None
    manifest_sha256: str = ""
    files_checked: int = 0
    outputs_checked: int = 0
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
            "files_checked": self.files_checked,
            "outputs_checked": self.outputs_checked,
            "errors": self.errors,
            "chain": self.chain.as_dict() if self.chain else None,
            "job": self.job.as_dict() if self.job else None,
        }


def is_render_package(root: Path) -> bool:
    try:
        manifest = json.loads((root / "manifest.json").read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and manifest.get("format") == RENDER_PACKAGE_FORMAT


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as fh:
        for n, raw in enumerate(fh, start=1):
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                raise PackageFormatError(f"{path.name}:{n}: invalid JSON") from exc
            if not isinstance(obj, dict):
                raise PackageFormatError(f"{path.name}:{n}: expected an object")
            yield obj


def _sha256_file(path: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def verify_render_package(
    root: Path, outputs: Sequence[Path] = (), job_package: Path | None = None
) -> RenderPackageReport:
    """``outputs``: output files supplied for a package that references them by hash (matched by
    SHA-256). ``job_package``: the rendered job's custody package, verified and matched too."""
    report = RenderPackageReport()
    manifest_bytes = (root / "manifest.json").read_bytes()
    report.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != RENDER_PACKAGE_FORMAT:
        raise PackageFormatError(f"unsupported render package format {manifest.get('format')!r}")

    # 1. every file is exactly what the manifest says
    for name in RENDER_FILES:
        expected = manifest["files"].get(name)
        path = root / name
        if expected is None or not path.exists():
            report.errors.append(f"{name}: missing from package or manifest")
            continue
        digest, lines = hashlib.sha256(), 0
        with path.open("rb") as fh:
            for raw in fh:
                digest.update(raw)
                lines += 1
        if digest.hexdigest() != expected["sha256"] or lines != expected["lines"]:
            report.errors.append(f"{name}: does not match manifest (modified after export)")
    if report.errors:
        return report

    tenant_id, render_id, job_id = manifest["tenant_id"], manifest["render_id"], manifest["job_id"]
    verifier = ChainVerifier(tenant_id, render_id)

    # 2. anchors first, then events in order, each render batch with its file records
    for rec in _lines(root / "anchors.jsonl"):
        if rec.get("delete_marker"):
            verifier.add_hidden_anchor(rec["key"], rec["version_id"])
        else:
            verifier.add_anchor(
                Anchor(rec["key"], rec["version_id"], base64.b64decode(rec["body_b64"]))
            )
    files_iter = _lines(root / "files.jsonl")
    pending: dict[str, Any] | None = next(files_iter, None)
    records: list[dict[str, Any]] = []
    first: EventRecord | None = None
    for rec in _lines(root / "events.jsonl"):
        ev = EventRecord(rec["id"], rec["fields"], rec["prev_hash"], rec["event_hash"])
        first = first or ev
        batch: list[dict[str, Any]] = []
        if ev.event_type == RENDER_BATCH_EVENT:
            while pending is not None and pending.get("custody_event_id") == ev.id:
                batch.append(pending["record"])
                pending = next(files_iter, None)
        records.extend(batch)
        report.files_checked += len(batch)
        verifier.add_event(ev, files=batch)
    if pending is not None:
        report.errors.append(
            f"files.jsonl: file {pending.get('record', {}).get('ord')} is not part of any batch in order"
        )
    head = manifest.get("head")
    report.chain = verifier.finish(
        require_seal=bool(manifest.get("finalized")),
        expected_head=(head["seq"], head["hash"]) if head else None,
    )

    # 3. the stream is this render's, and its reference to the sealed job is the included seal
    reference = _check_start(report, first, render_id, job_id)
    seal = json.loads((root / "job_seal.json").read_bytes())
    if reference is not None:
        _check_seal(report, tenant_id, job_id, reference, seal)

    # 4. the output files
    _check_outputs(report, root, manifest, records, outputs)

    # 5. optionally, the job itself
    if job_package is not None:
        report.job = verify_package(job_package)
        jm = json.loads((job_package / "manifest.json").read_bytes())
        if jm.get("stream_id") != job_id or jm.get("tenant_id") != tenant_id:
            report.errors.append("job package: belongs to another job or tenant")
        elif reference is not None:
            ref_head = reference.get("head", {})
            if jm.get("head") != {"seq": ref_head.get("seq"), "hash": ref_head.get("hash")}:
                report.errors.append("job package: its head is not the head the render references")
            anchors = {
                (a["key"], a["version_id"]): a.get("body_b64")
                for a in _lines(job_package / "anchors.jsonl")
            }
            if anchors.get((seal.get("key"), seal.get("version_id"))) != seal.get("body_b64"):
                report.errors.append("job package: does not hold the referenced seal anchor")
    return report


def _check_start(
    report: RenderPackageReport, first: EventRecord | None, render_id: str, job_id: str
) -> dict[str, Any] | None:
    if first is None:
        report.errors.append("events.jsonl: the render stream is empty")
        return None
    payload = first.fields.get("payload", {})
    if first.event_type not in (RENDER_STARTED, RENDER_REFUSED):
        report.errors.append(
            f"the render stream starts with {first.event_type}, not render_started"
        )
        return None
    if payload.get("render_id") != render_id or first.fields.get("job_id") is not None:
        report.errors.append("the first event belongs to another render (or names a job stream)")
    if first.event_type == RENDER_REFUSED:
        if payload.get("job_id") != job_id:
            report.errors.append("render_refused names another job")
        return None
    reference = payload.get("job")
    if not isinstance(reference, dict) or reference.get("id") != job_id:
        report.errors.append("render_started does not reference this package's job")
        return None
    return reference


def _check_seal(
    report: RenderPackageReport,
    tenant_id: str,
    job_id: str,
    reference: dict[str, Any],
    seal: dict[str, Any],
) -> None:
    ref_seal, ref_head = reference.get("seal", {}), reference.get("head", {})
    if (seal.get("key"), seal.get("version_id")) != (
        ref_seal.get("key"),
        ref_seal.get("version_id"),
    ):
        report.errors.append("job_seal.json is not the seal anchor render_started references")
        return
    try:
        doc = json.loads(base64.b64decode(seal.get("body_b64") or "", validate=True))
    except (ValueError, binascii.Error):
        report.errors.append("job_seal.json: the anchor body is not valid")
        return
    want = {
        "format": ANCHOR_FORMAT,
        "tenant_id": tenant_id,
        "stream_id": job_id,
        "seq": ref_head.get("seq"),
        "event_hash": ref_head.get("hash"),
    }
    if doc != want:
        report.errors.append(
            "the job's seal anchor does not anchor the head render_started references"
        )
    seq = ref_head.get("seq")
    if not isinstance(seq, int) or seal.get("key") != anchor_key(tenant_id, job_id, seq):
        report.errors.append("the job's seal anchor key is not the anchor of the referenced head")


def _check_outputs(
    report: RenderPackageReport,
    root: Path,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    supplied: Sequence[Path],
) -> None:
    names = [str(r.get("name")) for r in records]
    if len(set(names)) != len(names):
        report.errors.append("files.jsonl: two output files share a name")
    out_dir = root / "outputs"
    if manifest.get("outputs_included"):
        present = {p.name for p in out_dir.iterdir()} if out_dir.is_dir() else set()
        for extra in sorted(present - set(names)):
            report.errors.append(f"outputs/{extra}: not an output file of this render")
        candidates: dict[str, Path | None] = {r["name"]: out_dir / str(r["name"]) for r in records}
    else:
        by_hash = {_sha256_file(p)[0]: p for p in supplied}
        candidates = {r["name"]: by_hash.get(str(r.get("sha256"))) for r in records}
    for r in records:
        path = candidates.get(r["name"])
        if path is None or not path.exists():
            report.errors.append(f"output {r['name']}: not in the package and not supplied")
            continue
        digest, size = _sha256_file(path)
        if (digest, size) != (r.get("sha256"), r.get("size")):
            report.errors.append(f"output {r['name']}: bytes do not match the recorded sha256/size")
        report.outputs_checked += 1
