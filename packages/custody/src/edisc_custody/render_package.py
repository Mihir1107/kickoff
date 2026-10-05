"""Standalone verification of a render package (ADR 0015 §14 and §19, ADR 0008). No database, network
or credentials: stdlib and pure ``edisc_core`` / ``edisc_custody`` modules only.

A package is a directory or the same files in a zip (the download endpoint's stream), verified in
place without extracting it (``edisc_custody.package_source``). Format ``edisc-render-package/2``:

- ``manifest.json``: tenant, render and job ids, whether the render is finalized, its head and seal
  time, and the SHA-256, line count and size of every file below (no export time: two downloads of
  one render are byte-identical);
- ``events.jsonl``: the render's custody stream; ``anchors.jsonl``: its WORM anchors as listed from the
  bucket's object versions (key, VersionId, SHA-256 and size of the body, or a delete marker);
- ``files.jsonl``: every output file record, in render order, with the ``render_files_batch`` event it
  belongs to;
- ``job_seal.json``: the sealed job's seal anchor (key, VersionId, SHA-256 and size);
- ``objects/<sha256>``: the anchor bodies and the job seal body, as read from WORM;
- ``outputs/<name>``: the output files themselves, unless the package references them by hash only
  (``outputs_included`` false; the expert supplies them with ``--file``).

Format ``/3`` (ADR 0015 §20) adds the render's natives, the attachments kept outside the ``.rsmf``
zips: ``natives.jsonl`` (one record per native: ord, SHA-256, size, storage key, VersionId, the ords
of the files that reference it, and its batch event), and ``natives/<sha256>`` when outputs are
embedded (else supplied with ``--file``, matched by SHA-256 like outputs).

Format ``/1`` (directories exported before §19) carries the anchor and seal bodies inline
(``body_b64``) and has no ``objects/``; ``/1`` and ``/2`` are still accepted.

Checked: the manifest against every file; nothing in the package that the manifest does not account
for; every object's SHA-256 and size; the render chain (hashes, links, anchors, its seal) with every
batch's Merkle root, count and position recomputed from its file records and ``render_completed``'s
totals and root over the batch roots; that the stream starts with ``render_started`` (or
``render_refused``) for this render, whose reference to the job (id, final head, seal key and
version) equals the included seal anchor, which anchors exactly that head; every output file's
SHA-256 and size; every native's SHA-256 and size, each batch's ``natives_root`` and the total; and
each ``.rsmf`` opened with the hardened reader (``edisc_custody.rsmf_check``, never loaded whole): the
natives its ``edisc.file_external`` references name are exactly those whose records list that file,
none is unlisted, and the placeholder of each agrees with its record. With the job's custody package
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

from edisc_custody.archive import ArchiveError
from edisc_custody.chain import (
    ANCHOR_FORMAT,
    Anchor,
    ChainVerifier,
    EventRecord,
    VerificationReport,
    anchor_key,
)
from edisc_custody.package import PackageFormatError, PackageReport, verify_package
from edisc_custody.package_source import (
    DirectorySource,
    PackageSource,
    ZipSource,
    lines,
    open_source,
    read_all,
)
from edisc_custody.render_files import RENDER_BATCH_EVENT, RENDER_REFUSED, RENDER_STARTED
from edisc_custody.rsmf_check import FileRange, RangeReader, RsmfCheckError, external_refs

RENDER_PACKAGE_FORMAT = "edisc-render-package/3"
RENDER_PACKAGE_FORMAT_2 = "edisc-render-package/2"
RENDER_PACKAGE_FORMAT_1 = "edisc-render-package/1"
RENDER_PACKAGE_FORMATS = (RENDER_PACKAGE_FORMAT_1, RENDER_PACKAGE_FORMAT_2, RENDER_PACKAGE_FORMAT)
RENDER_FILES_2 = ("events.jsonl", "files.jsonl", "anchors.jsonl", "job_seal.json")
RENDER_FILES = ("events.jsonl", "files.jsonl", "natives.jsonl", "anchors.jsonl", "job_seal.json")
MAX_OBJECT_BYTES = 1 << 20  # anchors and seals are small JSON documents


@dataclass
class RenderPackageReport:
    chain: VerificationReport | None = None
    job: PackageReport | None = None
    manifest_sha256: str = ""
    tolerated: list[str] = field(default_factory=list)  # OS metadata accepted by the option
    files_checked: int = 0
    outputs_checked: int = 0
    natives_checked: int = 0
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
            "natives_checked": self.natives_checked,
            "errors": self.errors,
            "tolerated": self.tolerated,
            "chain": self.chain.as_dict() if self.chain else None,
            "job": self.job.as_dict() if self.job else None,
        }


def is_render_package(path: Path) -> bool:
    try:
        source = open_source(path)
        try:
            manifest = json.loads(read_all(source, "manifest.json"))
        finally:
            if isinstance(source, ZipSource):
                source.close()
    except (OSError, ValueError, ArchiveError, KeyError):
        return False
    return isinstance(manifest, dict) and manifest.get("format") in RENDER_PACKAGE_FORMATS


def _lines(source: PackageSource, name: str) -> Iterator[dict[str, Any]]:
    for n, raw in enumerate(lines(source, name), start=1):
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise PackageFormatError(f"{name}:{n}: invalid JSON") from exc
        if not isinstance(obj, dict):
            raise PackageFormatError(f"{name}:{n}: expected an object")
        yield obj


def _sha256_file(path: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _sha256_entry(source: PackageSource, name: str) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    for chunk in source.chunks(name):
        h.update(chunk)
        size += len(chunk)
    return h.hexdigest(), size


def is_os_metadata(name: str) -> bool:
    """Files an operating system may drop into an extracted folder: ``.DS_Store``, AppleDouble
    ``._*``, anything under ``__MACOSX/``, ``Thumbs.db``, ``desktop.ini``. Nothing else."""
    parts = name.split("/")
    base = parts[-1]
    return (
        "__MACOSX" in parts[:-1]
        or base in (".DS_Store", "Thumbs.db", "desktop.ini")
        or base.startswith("._")
    )


def verify_render_package(
    root: Path,
    outputs: Sequence[Path] = (),
    job_package: Path | None = None,
    *,
    tolerate_os_metadata: bool = False,
) -> RenderPackageReport:
    """``root``: the package directory or zip. ``outputs``: output files supplied for a package that
    references them by hash (matched by SHA-256). ``job_package``: the rendered job's custody
    package, verified and matched too. Strict: any file the manifest does not account for fails.
    ``tolerate_os_metadata`` accepts only OS metadata files (``is_os_metadata``) in a DIRECTORY
    package, each listed in ``report.tolerated``; it never applies to a zip, which is what experts
    should verify (an extracted folder is a copy someone's file manager may have touched)."""
    source = open_source(root)
    tolerate = tolerate_os_metadata and not isinstance(source, ZipSource)
    try:
        return _verify(source, outputs, job_package, tolerate)
    finally:
        if isinstance(source, ZipSource):
            source.close()


def _verify(
    source: PackageSource, outputs: Sequence[Path], job_package: Path | None, tolerate: bool
) -> RenderPackageReport:
    report = RenderPackageReport()
    manifest_bytes = read_all(source, "manifest.json")
    report.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    fmt = manifest.get("format")
    if fmt not in RENDER_PACKAGE_FORMATS:
        raise PackageFormatError(f"unsupported render package format {fmt!r}")
    v2 = fmt != RENDER_PACKAGE_FORMAT_1
    v3 = fmt == RENDER_PACKAGE_FORMAT
    jsonl_files = RENDER_FILES if v3 else RENDER_FILES_2

    # 1. every file is exactly what the manifest says
    for name in jsonl_files:
        expected = manifest["files"].get(name)
        if expected is None or not source.exists(name):
            report.errors.append(f"{name}: missing from package or manifest")
            continue
        digest, count, size = hashlib.sha256(), 0, 0
        for raw in lines(source, name):
            digest.update(raw)
            count += 1
            size += len(raw)
        if (
            digest.hexdigest() != expected["sha256"]
            or count != expected["lines"]
            or (v2 and size != expected.get("bytes"))
        ):
            report.errors.append(f"{name}: does not match manifest (modified after export)")
    if report.errors:
        return report

    tenant_id, render_id, job_id = manifest["tenant_id"], manifest["render_id"], manifest["job_id"]
    verifier = ChainVerifier(tenant_id, render_id)
    objects: set[str] = set()

    def body(rec: dict[str, Any], what: str) -> bytes | None:
        """An anchor's body: inline (/1) or ``objects/<sha256>`` checked against its record (/2)."""
        if not v2:
            try:
                return base64.b64decode(rec.get("body_b64") or "", validate=True)
            except (ValueError, binascii.Error):
                report.errors.append(f"{what}: the inline body is not valid base64")
                return None
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

    # 2. anchors first, then events in order, each render batch with its file records
    for rec in _lines(source, "anchors.jsonl"):
        if rec.get("delete_marker"):
            verifier.add_hidden_anchor(rec["key"], rec["version_id"])
            continue
        data = body(rec, f"anchor {rec.get('key')} (version {rec.get('version_id')})")
        if data is not None:
            verifier.add_anchor(Anchor(rec["key"], rec["version_id"], data))
    files_iter = _lines(source, "files.jsonl")
    natives_iter = _lines(source, "natives.jsonl") if v3 else iter(())
    pending: dict[str, Any] | None = next(files_iter, None)
    pending_native: dict[str, Any] | None = next(natives_iter, None)
    records: list[dict[str, Any]] = []
    natives: list[dict[str, Any]] = []
    first: EventRecord | None = None
    for rec in _lines(source, "events.jsonl"):
        ev = EventRecord(rec["id"], rec["fields"], rec["prev_hash"], rec["event_hash"])
        first = first or ev
        batch: list[dict[str, Any]] = []
        batch_natives: list[dict[str, Any]] = []
        if ev.event_type == RENDER_BATCH_EVENT:
            while pending is not None and pending.get("custody_event_id") == ev.id:
                batch.append(pending["record"])
                pending = next(files_iter, None)
            while pending_native is not None and pending_native.get("custody_event_id") == ev.id:
                batch_natives.append(pending_native["record"])
                pending_native = next(natives_iter, None)
        records.extend(batch)
        natives.extend(batch_natives)
        report.files_checked += len(batch)
        verifier.add_event(ev, files=batch, natives=batch_natives)
    if pending is not None:
        report.errors.append(
            f"files.jsonl: file {pending.get('record', {}).get('ord')} is not part of any batch in order"
        )
    if pending_native is not None:
        report.errors.append(
            f"natives.jsonl: native {pending_native.get('record', {}).get('ord')} is not part of any"
            " batch in order"
        )
    head = manifest.get("head")
    report.chain = verifier.finish(
        require_seal=bool(manifest.get("finalized")),
        expected_head=(head["seq"], head["hash"]) if head else None,
    )

    # 3. the stream is this render's, and its reference to the sealed job is the included seal
    reference = _check_start(report, first, render_id, job_id)
    seal = json.loads(read_all(source, "job_seal.json"))
    seal_body = body(seal, "job_seal.json") if seal.get("key") is not None else None
    if reference is not None:
        _check_seal(report, tenant_id, job_id, reference, seal, seal_body)

    # 4. the output files and natives, and nothing the manifest does not account for
    supplied = {} if manifest.get("outputs_included") else {_sha256_file(p)[0]: p for p in outputs}
    _check_outputs(report, source, manifest, records, supplied)
    if v3:
        _check_natives(report, source, manifest, records, natives, supplied)
    allowed = {"manifest.json", *jsonl_files, *objects}
    if manifest.get("outputs_included"):
        allowed |= {f"outputs/{r.get('name')}" for r in records}
        allowed |= {f"natives/{n.get('sha256')}" for n in natives}
    for extra in sorted(set(source.names()) - allowed):
        if tolerate and is_os_metadata(extra):
            report.tolerated.append(extra)
        elif extra.startswith("outputs/"):
            report.errors.append(f"{extra}: not an output file of this render")
        elif extra.startswith("natives/"):
            report.errors.append(f"{extra}: not a native of this render")
        else:
            report.errors.append(f"{extra}: not part of this package")

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
                for a in _lines(open_source(job_package), "anchors.jsonl")
            }
            held = anchors.get((seal.get("key"), seal.get("version_id")))
            if seal_body is None or held is None or base64.b64decode(held) != seal_body:
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
    body: bytes | None,
) -> None:
    ref_seal, ref_head = reference.get("seal", {}), reference.get("head", {})
    if (seal.get("key"), seal.get("version_id")) != (
        ref_seal.get("key"),
        ref_seal.get("version_id"),
    ):
        report.errors.append("job_seal.json is not the seal anchor render_started references")
        return
    try:
        doc = json.loads(body) if body is not None else None
    except ValueError:
        doc = None
    if doc is None:
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
    source: PackageSource,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    by_hash: dict[str, Path],
) -> None:
    names = [str(r.get("name")) for r in records]
    if len(set(names)) != len(names):
        report.errors.append("files.jsonl: two output files share a name")
    for r in records:
        name = str(r["name"])
        embedded = manifest.get("outputs_included")
        path = None if embedded else by_hash.get(str(r.get("sha256")))
        if (embedded and not source.exists(f"outputs/{name}")) or (not embedded and path is None):
            report.errors.append(f"output {name}: not in the package and not supplied")
            continue
        try:
            digest, size = (
                _sha256_entry(source, f"outputs/{name}") if path is None else _sha256_file(path)
            )
        except (
            ArchiveError
        ) as exc:  # a damaged zip entry is a failed check, not an unreadable package
            report.errors.append(f"output {name}: {exc}")
            continue
        if (digest, size) != (r.get("sha256"), r.get("size")):
            report.errors.append(f"output {name}: bytes do not match the recorded sha256/size")
        report.outputs_checked += 1


def _range_of(source: PackageSource, name: str, path: Path | None) -> tuple[RangeReader, Any]:
    """Random access to an output file: supplied, in a directory, or a STORED entry of the zip."""
    if path is not None:
        r = FileRange(path)
        return r, r
    if isinstance(source, DirectorySource):
        r = FileRange(source.root / name)
        return r, r
    if isinstance(source, ZipSource):
        return source.stored_range(name), None
    raise PackageFormatError(f"{name}: no random access to this package")


def _check_natives(
    report: RenderPackageReport,
    source: PackageSource,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    natives: list[dict[str, Any]],
    by_hash: dict[str, Path],
) -> None:
    """Every native's bytes, and every `.rsmf`'s references to natives against their records."""
    embedded = bool(manifest.get("outputs_included"))
    by_sha: dict[str, dict[str, Any]] = {}
    expected: dict[int, set[str]] = {}
    for n in natives:
        sha = str(n.get("sha256"))
        if sha in by_sha:
            report.errors.append(f"natives.jsonl: native {sha} listed twice")
        by_sha[sha] = n
        for o in n.get("file_ords") or ():
            expected.setdefault(o, set()).add(sha)
        name = f"natives/{sha}"
        path = None if embedded else by_hash.get(sha)
        if (embedded and not source.exists(name)) or (not embedded and path is None):
            report.errors.append(f"native {sha}: not in the package and not supplied")
            continue
        try:
            digest, size = _sha256_entry(source, name) if path is None else _sha256_file(path)
        except ArchiveError as exc:
            report.errors.append(f"native {sha}: {exc}")
            continue
        if (digest, size) != (sha, n.get("size")):
            report.errors.append(f"native {sha}: bytes do not match the recorded sha256/size")
        report.natives_checked += 1
    for r in records:
        name, ord_ = str(r.get("name")), r.get("ord")
        path = None if embedded else by_hash.get(str(r.get("sha256")))
        if (embedded and not source.exists(f"outputs/{name}")) or (not embedded and path is None):
            continue  # reported by _check_outputs
        closer = None
        try:
            reader, closer = _range_of(source, f"outputs/{name}", path)
            refs = external_refs(reader)
        except (RsmfCheckError, ArchiveError, PackageFormatError, OSError) as exc:
            report.errors.append(f"output {name}: its native references cannot be read ({exc})")
            continue
        finally:
            if closer is not None:
                closer.close()
        named = {ref.sha256 for ref in refs}
        for ref in refs:
            known = by_sha.get(ref.sha256)
            if known is None:
                report.errors.append(
                    f"output {name}: names native {ref.sha256}, which is not listed"
                )
            elif known.get("size") != ref.size:
                report.errors.append(f"output {name}: native {ref.sha256} with another size")
        if "external_count" in r and r["external_count"] != len(refs):
            report.errors.append(f"output {name}: external_count differs from its placeholders")
        listed = expected.get(ord_, set()) if isinstance(ord_, int) else set()
        if named != listed:
            report.errors.append(
                f"output {name}: references natives {sorted(named)[:3]}, the records list"
                f" {sorted(listed)[:3]} for it"
            )
