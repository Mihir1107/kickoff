"""Standalone custody package verification (ADR 0008). No database, network or credentials.

Imports only the standard library and pure ``edisc_core`` / ``edisc_custody`` modules, so a third-party
expert can run ``edisc-verify <dir>`` on an air-gapped machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from edisc_core.canonical import canonical_hash
from edisc_core.idempotency import idempotency_key
from edisc_core.jsonpath import JsonPathError, resolve
from edisc_custody.chain import BATCH_EVENT, Anchor, ChainVerifier, EventRecord, VerificationReport

PACKAGE_FORMAT = "edisc-custody-package/1"
FILES = ("events.jsonl", "items.jsonl", "evidence.jsonl", "anchors.jsonl")


# ------------------------------------------------------------------ standalone verification
@dataclass
class PackageReport:
    chain: VerificationReport | None = None
    manifest_sha256: str = ""
    items_checked: int = 0
    objects_checked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and (self.chain is not None and self.chain.ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "manifest_sha256": self.manifest_sha256,
            "items_checked": self.items_checked,
            "objects_checked": self.objects_checked,
            "errors": self.errors,
            "chain": self.chain.as_dict() if self.chain else None,
        }


class PackageFormatError(ValueError):
    pass


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


def verify_package(root: Path) -> PackageReport:
    """Verify an exported package with no database or network access."""
    report = PackageReport()
    manifest_bytes = (root / "manifest.json").read_bytes()
    report.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != PACKAGE_FORMAT:
        raise PackageFormatError(f"unsupported package format {manifest.get('format')!r}")

    # 1. every file is exactly what the manifest says
    for name in FILES:
        expected = manifest["files"].get(name)
        path = root / name
        if expected is None or not path.exists():
            report.errors.append(f"{name}: missing from package or manifest")
            continue
        data_hash, lines = hashlib.sha256(), 0
        with path.open("rb") as fh:
            for raw in fh:
                data_hash.update(raw)
                lines += 1
        if data_hash.hexdigest() != expected["sha256"] or lines != expected["lines"]:
            report.errors.append(f"{name}: does not match manifest (modified after export)")
    if report.errors:
        return report

    tenant_id, stream_id = manifest["tenant_id"], manifest["stream_id"]
    verifier = ChainVerifier(tenant_id, stream_id)

    # 2. anchors first
    for rec in _lines(root / "anchors.jsonl"):
        if rec.get("delete_marker"):
            verifier.add_hidden_anchor(rec["key"], rec["version_id"])
        else:
            verifier.add_anchor(
                Anchor(rec["key"], rec["version_id"], base64.b64decode(rec["body_b64"]))
            )

    # 3. evidence registry (small): id -> record, and object bytes if included
    evidence: dict[str, dict[str, Any]] = {}
    objects_dir = root / "objects"
    for rec in _lines(root / "evidence.jsonl"):
        evidence[rec["id"]] = rec
        if (
            manifest.get("objects_included")
            and rec.get("state") == "complete"
            and rec.get("kind") in ("page", "file")
        ):
            path = objects_dir / str(rec["sha256"])
            if not path.exists():
                report.errors.append(
                    f"evidence {rec['storage_key']}: object bytes missing from package"
                )
                continue
            if _sha256_file(path) != rec["sha256"] or path.stat().st_size != rec["size_bytes"]:
                report.errors.append(
                    f"evidence {rec['storage_key']}: bytes do not match recorded sha256/size"
                )
            report.objects_checked += 1

    # 4. events in order, each batch with its items (items.jsonl is grouped by batch, in seq order)
    items_iter = _lines(root / "items.jsonl")
    pending: dict[str, Any] | None = next(items_iter, None)
    page_cache: tuple[str, Any] | None = None
    for rec in _lines(root / "events.jsonl"):
        ev = EventRecord(rec["id"], rec["fields"], rec["prev_hash"], rec["event_hash"])
        pairs: list[tuple[str, str]] = []
        if ev.event_type == BATCH_EVENT:
            while pending is not None and pending.get("custody_event_id") == ev.id:
                page_cache = _check_item(
                    report, manifest, evidence, objects_dir, pending, page_cache
                )
                pairs.append((pending["idempotency_key"], pending["content_hash"]))
                pending = next(items_iter, None)
        verifier.add_event(ev, pairs)
    if pending is not None:
        report.errors.append(
            f"items.jsonl: item {pending.get('id')} is not linked to any batch event in order"
        )

    head = manifest.get("head")
    expected_head = (head["seq"], head["hash"]) if head else None
    report.chain = verifier.finish(
        require_seal=bool(manifest.get("finalized")), expected_head=expected_head
    )
    return report


def _check_item(
    report: PackageReport,
    manifest: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    objects_dir: Path,
    item: dict[str, Any],
    page_cache: tuple[str, Any] | None,
) -> tuple[str, Any] | None:
    report.items_checked += 1
    where = f"item {item.get('id')}"
    try:
        expected_key = idempotency_key(
            manifest["tenant_id"], item["source"], item["source_item_id"], item["content_hash"]
        )
    except (KeyError, ValueError) as exc:
        report.errors.append(f"{where}: cannot recompute idempotency key: {exc}")
        return page_cache
    if expected_key != item["idempotency_key"]:
        report.errors.append(f"{where}: idempotency_key does not match its components")
    ev = evidence.get(str(item["evidence_object_id"]))
    if ev is None:
        report.errors.append(
            f"{where}: evidence object {item['evidence_object_id']} not in package"
        )
        return page_cache
    if ev["storage_key"] != item["storage_key"]:
        report.errors.append(f"{where}: storage_key disagrees with evidence registry")
    if not manifest.get("objects_included"):
        return page_cache
    path = objects_dir / str(ev["sha256"])
    if not path.exists():
        return page_cache  # already reported under evidence
    if ev["kind"] == "file":
        if item["raw_hash"] != ev["sha256"]:
            report.errors.append(f"{where}: raw_hash differs from the file object's sha256")
        return page_cache
    if page_cache is None or page_cache[0] != ev["sha256"]:
        page_cache = (ev["sha256"], json.loads(path.read_bytes()))
    try:
        fragment = resolve(page_cache[1], item["json_path"])
    except JsonPathError as exc:
        report.errors.append(f"{where}: json_path does not resolve in its page: {exc}")
        return page_cache
    if canonical_hash(fragment) != item["raw_hash"]:
        report.errors.append(
            f"{where}: raw_hash does not match the page fragment at {item['json_path']}"
        )
    return page_cache


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
