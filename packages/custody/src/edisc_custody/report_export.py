"""Report packages (``edisc-report-package/1``, ADR 0018 §14, §27): one generator for the directory
export and the download stream (``GET /v1/reports/{id}/package``), verified offline by
``edisc-verify`` (``edisc_verify.report_package``).

Entries, in a fixed order: ``manifest.json``; ``events.jsonl`` (the report's own custody stream up to
its sealed head); ``anchors.jsonl`` (the report stream's anchors as LISTED from the bucket, never the
database); ``job_seal.json`` (the job seal anchor ``report_started`` references); ``objects/<sha256>``
(anchor and seal bodies); ``outputs/<name>`` (every report file: they are bounded, so always
embedded). The file records are those of the sealed ``report_generated`` event, so no separate
listing is needed. Same two passes as render packages (``edisc_custody.render_export``): the plan
reads no output body and knows the zip's exact size; the members re-read everything by pinned
VersionId and check it against the plan as it streams (``PackageIntegrityError``). The manifest has
no export time (the seal time instead): two downloads of one report are byte-identical.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_core.time import format_utc
from edisc_custody.render_export import (
    PackageIntegrityError,
    PackageObject,
    _aiter,
    _divergence,
    _event_lines,
    _jsonl_member,
    _jsonl_spec,
    _object_member,
    _registry,
)
from edisc_custody.zipwriter import ZipMember, ZipSizer
from edisc_db.session import tenant_tx
from edisc_evidence.worm import get_bytes, list_versions
from edisc_verify.canonical import canonical_json
from edisc_verify.chain import anchor_prefix
from edisc_verify.report_files import REPORT_GENERATED, REPORT_STARTED
from edisc_verify.report_package import REPORT_PACKAGE_FORMAT

__all__ = [
    "PackageIntegrityError",
    "ReportPackagePlan",
    "export_report_package",
    "plan_report_package",
    "report_package_members",
]  # fmt: skip

_JSONL_ORDER = ("events.jsonl", "anchors.jsonl", "job_seal.json")


@dataclass(frozen=True)
class ReportOutput:
    name: str
    key: str
    version_id: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ReportPackagePlan:
    tenant_id: uuid.UUID
    report_id: uuid.UUID
    job_id: uuid.UUID
    manifest: bytes
    head_seq: int
    files: dict[str, dict[str, Any]]
    anchors: tuple[dict[str, Any], ...]
    seal: dict[str, Any]
    objects: tuple[PackageObject, ...]
    outputs: tuple[ReportOutput, ...]
    page_size: int = 1000
    zip_size: int = 0
    anchor_divergences: tuple[dict[str, Any], ...] = ()

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.manifest).hexdigest()


async def plan_report_package(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    report_id: uuid.UUID,
    page_size: int = 1000,
) -> ReportPackagePlan:
    bucket = settings.s3_evidence_bucket
    async with tenant_tx(sessions, tenant_id) as s:
        report = (
            await s.execute(
                text(
                    "SELECT r.job_id, r.status, r.sealed_at, h.last_seq, h.last_hash"
                    " FROM reports r LEFT JOIN custody_chain_heads h ON h.stream_id = r.id"
                    " WHERE r.id = :r"
                ),
                {"r": report_id},
            )
        ).one()
        db_anchors = {
            r.storage_key: r
            for r in (
                await s.execute(
                    text(
                        "SELECT storage_key, version_id, sha256, size_bytes, state FROM evidence_objects"
                        " WHERE report_id = :r AND kind = 'anchor'"
                    ),
                    {"r": report_id},
                )
            ).all()
        }
        stored = {
            r.name: r
            for r in (
                await s.execute(
                    text(
                        "SELECT f.name, f.version_id, f.sha256, f.size_bytes, e.storage_key,"
                        " e.version_id AS registry_version, e.sha256 AS registry_sha, e.state"
                        " FROM report_files f JOIN evidence_objects e ON e.id = f.evidence_object_id"
                        " WHERE f.report_id = :r"
                    ),
                    {"r": report_id},
                )
            ).all()
        }
    head_seq = int(report.last_seq or 0)

    job_ref: dict[str, Any] | None = None
    generated: list[dict[str, Any]] = []
    async for line in _event_lines(sessions, tenant_id, report_id, head_seq, page_size):
        kind = line["fields"]["event_type"]
        if kind == REPORT_STARTED:
            job_ref = dict(line["fields"]["payload"]["job"])
        elif kind == REPORT_GENERATED:
            generated = list(line["fields"]["payload"]["files"])

    # the outputs: the sealed records (report_generated), each matched with its registry row
    outputs: list[ReportOutput] = []
    for rec in generated:
        row = stored.get(rec["name"])
        if (
            row is None
            or row.state != "complete"
            or (row.sha256, row.size_bytes, row.version_id) != (rec["sha256"], rec["size"],
                                                                rec["version_id"])
            or (row.registry_sha, row.registry_version) != (row.sha256, row.version_id)
        ):  # fmt: skip
            raise PackageIntegrityError(
                f"outputs/{rec['name']}", "the registry disagrees with the record"
            )
        outputs.append(
            ReportOutput(rec["name"], row.storage_key, row.version_id, row.sha256, row.size_bytes)
        )

    anchors: list[dict[str, Any]] = []
    objects: dict[str, PackageObject] = {}
    divergences: list[dict[str, Any]] = []
    listed: set[tuple[str, str]] = set()
    async for v in list_versions(
        s3, bucket=bucket, prefix=anchor_prefix(str(tenant_id), str(report_id))
    ):
        listed.add((v.key, v.version_id))
        row = db_anchors.get(v.key)
        if v.is_delete_marker:
            anchors.append({"key": v.key, "version_id": v.version_id, "delete_marker": True})
            divergences.append(_divergence("delete_marker", v.key, v.version_id, row))
            continue
        body = await get_bytes(s3, bucket=bucket, key=v.key, version_id=v.version_id)
        obj = PackageObject(v.key, v.version_id, hashlib.sha256(body).hexdigest(), len(body))
        objects.setdefault(obj.sha256, obj)
        anchors.append({"key": v.key, "version_id": v.version_id, "sha256": obj.sha256,
                        "size": obj.size})  # fmt: skip
        if row is None:
            divergences.append(_divergence("missing_row", v.key, v.version_id, None, obj))
        elif row.version_id != v.version_id:
            divergences.append(_divergence("extra_version", v.key, v.version_id, row, obj))
        elif (row.sha256, row.size_bytes) != (obj.sha256, obj.size):
            divergences.append(_divergence("hash_mismatch", v.key, v.version_id, row, obj))
    for key, row in sorted(db_anchors.items()):
        if (key, row.version_id) not in listed:
            divergences.append(_divergence("missing_object", key, row.version_id, row))

    seal: dict[str, Any] = {"key": None, "version_id": None, "sha256": None, "size": None}
    seal_ref = job_ref.get("seal") if job_ref else None
    if seal_ref:
        known = (await _registry(sessions, tenant_id, [seal_ref["key"]])).get(
            (seal_ref["key"], seal_ref["version_id"])
        )
        if known is None:  # a storage incident the verifier must see: read the version once
            body = await get_bytes(s3, bucket=bucket, key=seal_ref["key"],
                                   version_id=seal_ref["version_id"])  # fmt: skip
            known = (hashlib.sha256(body).hexdigest(), len(body))
        obj = PackageObject(seal_ref["key"], seal_ref["version_id"], known[0], known[1])
        objects.setdefault(obj.sha256, obj)
        seal = {"key": obj.key, "version_id": obj.version_id, "sha256": obj.sha256,
                "size": obj.size}  # fmt: skip

    files = {
        "events.jsonl": await _jsonl_spec(
            _event_lines(sessions, tenant_id, report_id, head_seq, page_size)
        ),
        "anchors.jsonl": await _jsonl_spec(_aiter(anchors)),
        "job_seal.json": await _jsonl_spec(_aiter([seal])),
    }
    manifest = {
        "format": REPORT_PACKAGE_FORMAT,
        "tenant_id": str(tenant_id),
        "report_id": str(report_id),
        "job_id": str(report.job_id),
        "status": report.status,
        "head": {"seq": report.last_seq, "hash": report.last_hash} if report.last_seq else None,
        "sealed_at": format_utc(report.sealed_at) if report.sealed_at else None,
        "files": files,
        "outputs": [{"name": o.name, "sha256": o.sha256, "size": o.size} for o in outputs],
    }
    manifest_bytes = canonical_json(manifest)
    sizer = ZipSizer()
    sizer.add("manifest.json", len(manifest_bytes))
    for name in _JSONL_ORDER:
        sizer.add(name, files[name]["bytes"])
    for sha in sorted(objects):
        sizer.add(f"objects/{sha}", objects[sha].size)
    for o in outputs:
        sizer.add(f"outputs/{o.name}", o.size)
    return ReportPackagePlan(
        tenant_id=tenant_id, report_id=report_id, job_id=report.job_id, manifest=manifest_bytes,
        head_seq=head_seq, files=files, anchors=tuple(anchors), seal=seal,
        objects=tuple(objects[sha] for sha in sorted(objects)), outputs=tuple(outputs),
        page_size=page_size, zip_size=sizer.total(), anchor_divergences=tuple(divergences),
    )  # fmt: skip


async def report_package_members(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    plan: ReportPackagePlan,
) -> AsyncIterator[ZipMember]:
    """The package entries in their fixed order, each checked against the plan as it streams."""
    t, r, n = plan.tenant_id, plan.report_id, plan.page_size
    bucket = settings.s3_evidence_bucket

    async def manifest() -> AsyncIterator[bytes]:
        yield plan.manifest

    yield ZipMember("manifest.json", len(plan.manifest), manifest)
    yield _jsonl_member(
        "events.jsonl",
        plan.files["events.jsonl"],
        lambda: _event_lines(sessions, t, r, plan.head_seq, n),
    )
    yield _jsonl_member(
        "anchors.jsonl", plan.files["anchors.jsonl"], lambda: _aiter(list(plan.anchors))
    )
    yield _jsonl_member("job_seal.json", plan.files["job_seal.json"], lambda: _aiter([plan.seal]))
    for obj in plan.objects:
        yield _object_member(
            s3, bucket, f"objects/{obj.sha256}", obj.key, obj.version_id, obj.sha256, obj.size
        )
    for o in plan.outputs:
        yield _object_member(s3, bucket, f"outputs/{o.name}", o.key, o.version_id, o.sha256, o.size)


async def export_report_package(
    sessions: async_sessionmaker[AsyncSession],
    s3: S3Client,
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    report_id: uuid.UUID,
    dest: Path,
) -> Path:
    """Write the package of one report into ``dest`` (must not exist): the same entries, with the
    same bytes, as the zip the download endpoint streams."""
    plan = await plan_report_package(sessions, s3, settings, tenant_id=tenant_id,
                                     report_id=report_id)  # fmt: skip
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=False)
    async for member in report_package_members(sessions, s3, settings, plan):
        path = dest / member.name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            async for chunk in member.chunks():
                await asyncio.to_thread(fh.write, chunk)
    return dest
