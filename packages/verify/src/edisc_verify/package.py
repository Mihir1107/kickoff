"""Standalone custody package verification (ADR 0008). No database, network or credentials.

Imports only the standard library, this distribution and its pinned dependencies, so a third-party
expert can run ``edisc-verify <dir>`` on an air-gapped machine.

Format ``/2`` (ADR 0014 section 2) adds archives: evidence that is an ENTRY inside a locked export zip.
A package carries each archive either **embedded** (``objects/<sha256>``) or **referenced** by its
SHA-256 and size only, for archives too large to ship (the expert passes the file with ``--archive``).
Either way the archive's SHA-256 and size are checked FIRST; only an archive that matches is opened.
Then every entry is found by its exact name bytes in the central directory (duplicates rejected), its
recorded CRC-32 and compressed size are compared with the directory, and it is decompressed under the
export's limits with local header, CRC-32, size and SHA-256 checked, before item fragments are
checked against it like any page. Format ``/1`` packages verify as before.

A package is a directory or the downloaded zip, read IN PLACE through ``package_source`` (the hardened
zip reader: nothing is extracted or copied). It is strict: every file must be one the manifest
accounts for (the four JSONL files, ``objects/<sha256>`` of each embedded object and archive), else
the package fails. ``tolerate_os_metadata`` accepts OS metadata files (``.DS_Store``, ``._*``,
``__MACOSX/``, ``Thumbs.db``, ``desktop.ini``) in a DIRECTORY only, each listed; never in a zip.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from edisc_verify.canonical import canonical_source_hash
from edisc_verify.chain import BATCH_EVENT, Anchor, ChainVerifier, EventRecord, VerificationReport
from edisc_verify.idempotency import idempotency_key
from edisc_verify.jsonpath import JsonPathError, resolve
from edisc_verify.package_archives import ArchiveEntries
from edisc_verify.package_source import (
    PackageSource,
    ZipSource,
    is_os_metadata,
    lines,
    open_source,
    read_all,
)

PACKAGE_FORMAT = "edisc-custody-package/3"  # /2 with a deterministic manifest (seal time, §27)
SUPPORTED_FORMATS = ("edisc-custody-package/1", "edisc-custody-package/2", PACKAGE_FORMAT)
FILES = ("events.jsonl", "items.jsonl", "evidence.jsonl", "anchors.jsonl")
MAX_PAGE_BYTES = 1 << 30  # a page object parsed as JSON (pages are bounded API responses)


# ------------------------------------------------------------------ standalone verification
@dataclass
class PackageReport:
    chain: VerificationReport | None = None
    manifest_sha256: str = ""
    items_checked: int = 0
    objects_checked: int = 0
    archives_checked: int = 0
    entries_checked: int = 0
    errors: list[str] = field(default_factory=list)
    tolerated: list[str] = field(default_factory=list)  # OS metadata accepted (directories only)

    @property
    def ok(self) -> bool:
        return not self.errors and (self.chain is not None and self.chain.ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "manifest_sha256": self.manifest_sha256,
            "items_checked": self.items_checked,
            "objects_checked": self.objects_checked,
            "archives_checked": self.archives_checked,
            "entries_checked": self.entries_checked,
            "errors": self.errors,
            "tolerated": self.tolerated,
            "chain": self.chain.as_dict() if self.chain else None,
        }


class PackageFormatError(ValueError):
    pass


def _lines(source: PackageSource, name: str) -> Iterator[dict[str, Any]]:
    for n, raw in enumerate(lines(source, name), start=1):
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise PackageFormatError(f"{name}:{n}: invalid JSON") from exc
        if not isinstance(obj, dict):
            raise PackageFormatError(f"{name}:{n}: expected an object")
        yield obj


def read_manifest(path: Path) -> dict[str, Any]:
    """A job package's manifest (directory or zip)."""
    source = open_source(path)
    try:
        manifest = json.loads(read_all(source, "manifest.json"))
    finally:
        if isinstance(source, ZipSource):
            source.close()
    if not isinstance(manifest, dict):
        raise PackageFormatError("manifest.json: expected an object")
    return manifest


def verify_package(
    path: Path, archives: Sequence[Path] = (), *, tolerate_os_metadata: bool = False
) -> PackageReport:
    """Verify an exported package (a directory, or the zip read in place) with no database or network
    access. ``archives``: files supplied for archives the package references by hash instead of
    embedding (matched by their SHA-256)."""
    source = open_source(path)
    try:
        tolerate = tolerate_os_metadata and not isinstance(source, ZipSource)
        return _verify(source, archives, tolerate)
    finally:
        if isinstance(source, ZipSource):
            source.close()


def _verify(source: PackageSource, archives: Sequence[Path], tolerate: bool) -> PackageReport:
    report = PackageReport()
    manifest_bytes = read_all(source, "manifest.json")
    report.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("format") not in SUPPORTED_FORMATS:
        fmt = manifest.get("format") if isinstance(manifest, dict) else None
        raise PackageFormatError(f"unsupported package format {fmt!r}")
    allowed = {"manifest.json", *FILES}

    # 1. every file is exactly what the manifest says
    for name in FILES:
        expected = manifest["files"].get(name)
        if expected is None or not source.exists(name):
            report.errors.append(f"{name}: missing from package or manifest")
            continue
        data_hash, count = hashlib.sha256(), 0
        for raw in lines(source, name):
            data_hash.update(raw)
            count += 1
        if data_hash.hexdigest() != expected["sha256"] or count != expected["lines"]:
            report.errors.append(f"{name}: does not match manifest (modified after export)")
    if report.errors:
        _strict(report, source, allowed, tolerate)
        return report

    tenant_id, stream_id = manifest["tenant_id"], manifest["stream_id"]
    verifier = ChainVerifier(tenant_id, stream_id)

    # 2. anchors first
    for rec in _lines(source, "anchors.jsonl"):
        if rec.get("delete_marker"):
            verifier.add_hidden_anchor(rec["key"], rec["version_id"])
        else:
            verifier.add_anchor(
                Anchor(rec["key"], rec["version_id"], base64.b64decode(rec["body_b64"]))
            )

    # 3. evidence registry (small): id -> record, and object bytes if included
    evidence: dict[str, dict[str, Any]] = {}
    # archives are checked (hash first) by ArchiveEntries, embedded or supplied: not as plain objects
    archive_ids = {str(a["evidence_id"]) for a in manifest.get("archives") or []}
    allowed |= {
        f"objects/{a['sha256']}" for a in manifest.get("archives") or [] if a.get("embedded")
    }
    for rec in _lines(source, "evidence.jsonl"):
        evidence[rec["id"]] = rec
        if rec.get("state") == "complete" and not rec.get("version_id"):
            report.errors.append(
                f"evidence {rec['storage_key']}: complete but no pinned version_id"
            )
        if rec.get("state") == "complete" and rec.get("sha256") != rec.get("source_sha256"):
            report.errors.append(
                f"evidence {rec['storage_key']}: stored hash differs from the source hash"
            )
        if rec.get("shadow_versions"):
            report.errors.append(
                f"storage incident: {rec['storage_key']} has version(s) {rec['shadow_versions']} besides the"
                f" pinned {rec.get('version_id')}; the pinned version is what was exported and verified"
            )
        if (
            manifest.get("objects_included")
            and rec.get("state") == "complete"
            and rec.get("kind") in ("page", "file")
            and str(rec["id"]) not in archive_ids
        ):
            name = f"objects/{rec['sha256']}"
            allowed.add(name)
            if not source.exists(name):
                report.errors.append(
                    f"evidence {rec['storage_key']}: object bytes missing from package"
                )
                continue
            if _sha256(source, name) != (rec["sha256"], rec["size_bytes"]):
                report.errors.append(
                    f"evidence {rec['storage_key']}: bytes do not match recorded sha256/size"
                )
            report.objects_checked += 1

    # 3b. archives: hash first, then every entry the package references (format /2)
    entries = ArchiveEntries(source, manifest, evidence, archives, report)

    # 4. events in order, each batch with its items (items.jsonl is grouped by batch, in seq order)
    items_iter = _lines(source, "items.jsonl")
    pending: dict[str, Any] | None = next(items_iter, None)
    page_cache: tuple[str, Any] | None = None
    for rec in _lines(source, "events.jsonl"):
        ev = EventRecord(rec["id"], rec["fields"], rec["prev_hash"], rec["event_hash"])
        pairs: list[tuple[str, str]] = []
        if ev.event_type == BATCH_EVENT:
            while pending is not None and pending.get("custody_event_id") == ev.id:
                page_cache = _check_item(
                    report, manifest, evidence, source, pending, page_cache, entries
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
    # 5. nothing the manifest does not account for
    _strict(report, source, allowed, tolerate)
    return report


def _strict(
    report: PackageReport, source: PackageSource, allowed: set[str], tolerate: bool
) -> None:
    """Every file not accounted for fails the package (OS metadata only in a directory, with the
    option, and listed)."""
    for extra in sorted(set(source.names()) - allowed):
        if tolerate and is_os_metadata(extra):
            report.tolerated.append(extra)
        elif extra.startswith("objects/"):
            report.errors.append(f"{extra}: not an evidence object of this package")
        else:
            report.errors.append(f"{extra}: not part of this package")


def _check_item(
    report: PackageReport,
    manifest: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    source: PackageSource,
    item: dict[str, Any],
    page_cache: tuple[str, Any] | None,
    entries: ArchiveEntries,
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
    if ev["kind"] == "archive_entry":
        body = entries.body(str(ev["id"]))
        if body is None:
            return page_cache  # the entry or its archive failed: already reported
        if page_cache is None or page_cache[0] != ev["sha256"]:
            page_cache = (ev["sha256"], json.loads(body))
        return _check_fragment(report, where, item, page_cache)
    if not manifest.get("objects_included"):
        return page_cache
    name = f"objects/{ev['sha256']}"
    if not source.exists(name):
        return page_cache  # already reported under evidence
    if ev["kind"] == "file":
        if item["raw_hash"] != ev["sha256"]:
            report.errors.append(f"{where}: raw_hash differs from the file object's sha256")
        return page_cache
    if page_cache is None or page_cache[0] != ev["sha256"]:
        page_cache = (ev["sha256"], json.loads(read_all(source, name, MAX_PAGE_BYTES)))
    return _check_fragment(report, where, item, page_cache)


def _check_fragment(
    report: PackageReport, where: str, item: dict[str, Any], page_cache: tuple[str, Any]
) -> tuple[str, Any]:
    try:
        fragment = resolve(page_cache[1], item["json_path"])
    except JsonPathError as exc:
        report.errors.append(f"{where}: json_path does not resolve in its page: {exc}")
        return page_cache
    if canonical_source_hash(fragment) != item["raw_hash"]:  # source data: lossless (ADR 0018 §23)
        report.errors.append(
            f"{where}: raw_hash does not match the page fragment at {item['json_path']}"
        )
    return page_cache


def _sha256(source: PackageSource, name: str) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    for chunk in source.chunks(name):
        h.update(chunk)
        size += len(chunk)
    return h.hexdigest(), size
