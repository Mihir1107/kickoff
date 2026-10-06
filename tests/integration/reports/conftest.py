"""Report workflow helpers (ADR 0018 §9, §13)."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from types_aiobotocore_s3 import S3Client

from edisc_core.settings import Settings
from edisc_custody.log import verify_chain
from edisc_custody.report_files import files_root
from edisc_db.session import tenant_tx
from edisc_worker.report_loader import ReportLoader
from edisc_worker.reports import ReportRun, create_report

from ..normalizer.harness import Sessions, Tenant


async def new_report(sessions: Sessions, t: Tenant, job_id: uuid.UUID, **kw: Any) -> uuid.UUID:
    async with tenant_tx(sessions, t.tenant_id) as s:
        created = await create_report(
            s, tenant_id=t.tenant_id, job_id=job_id, matter_id=t.matter_id,
            requested_by=kw.pop("requested_by", "user:tester"), **kw,
        )  # fmt: skip
    return created.report_id


async def drive(run: ReportRun, tenant_id: uuid.UUID, report_id: uuid.UUID) -> dict[str, Any]:
    """The workflow's sequence, without Temporal: snapshot, begin, files, complete; an ordinary
    failure goes to ``fail`` (as ReportWorkflow does). A SimulatedCrash (a BaseException) is a
    kill: it propagates."""
    try:
        status = await run.snapshot(tenant_id, report_id)
        if status == "snapshotted":
            status = await run.begin(tenant_id, report_id)
        if status == "generating":
            status = await run.files(tenant_id, report_id)
        return await run.complete(tenant_id, report_id)
    except Exception as exc:
        return await run.fail(tenant_id, report_id, type(exc).__name__, str(exc))


async def report_state(
    sessions: Sessions, tenant_id: uuid.UUID, report_id: uuid.UUID
) -> dict[str, Any]:
    async with tenant_tx(sessions, tenant_id) as s:
        row = (await s.execute(text("SELECT * FROM reports WHERE id = :r"), {"r": report_id})).one()
        events = (
            await s.execute(
                text(
                    "SELECT event_type, payload FROM custody_events WHERE stream_id = :r ORDER BY seq"
                ),
                {"r": report_id},
            )
        ).all()
        audits = (
            (
                await s.execute(
                    text(
                        "SELECT event_type FROM custody_events WHERE stream_id = :t"
                        " AND payload->>'report_id' = :r ORDER BY seq"
                    ),
                    {"t": tenant_id, "r": str(report_id)},
                )
            )
            .scalars()
            .all()
        )
        files = (
            await s.execute(
                text(
                    "SELECT ord, name, media_type, sha256, size_bytes AS size, rows, version_id,"
                    " evidence_object_id FROM report_files WHERE report_id = :r ORDER BY ord"
                ),
                {"r": report_id},
            )
        ).all()
        evidence = Counter(
            (
                await s.execute(
                    text(
                        "SELECT state FROM evidence_objects WHERE report_id = :r AND kind = 'report'"
                    ),
                    {"r": report_id},
                )
            ).scalars()
        )
    return {"row": row, "types": [e.event_type for e in events], "events": events,
            "audits": [a for a in audits if a != "audit.report_divergence"],
            "divergence_audits": [a for a in audits if a == "audit.report_divergence"],
            "files": files, "evidence": dict(evidence)}  # fmt: skip


async def expected_files(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, job_id: uuid.UUID,
    snapshot: dict[str, Any], identity: dict[str, Any], image_digest: str | None = None,
) -> dict[str, bytes]:  # fmt: skip
    """The oracle of the stored bytes: an independent in-memory build from the same snapshot."""
    out: dict[str, bytes] = {}

    async def sink(name: str, chunks: AsyncIterator[bytes]) -> None:
        out[name] = b"".join([c async for c in chunks])

    await ReportLoader(sessions, s3, settings, tenant_id=t.tenant_id, job_id=job_id).build(
        sink, snapshot=snapshot, identity=identity, image_digest=image_digest
    )
    return out


async def stored_bytes(
    s3: S3Client, settings: Settings, t: Tenant, report_id: uuid.UUID, files: Any
) -> dict[str, bytes]:
    out = {}
    for f in files:
        resp = await s3.get_object(
            Bucket=settings.s3_evidence_bucket, Key=f"t/{t.tenant_id}/reports/{report_id}/{f.name}",
            VersionId=f.version_id,
        )  # fmt: skip
        async with resp["Body"] as body:
            out[f.name] = await body.read()
        assert hashlib.sha256(out[f.name]).hexdigest() == f.sha256
    return out


async def assert_completed(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, job_id: uuid.UUID,
    report_id: uuid.UUID,
) -> dict[str, Any]:  # fmt: skip
    st = await report_state(sessions, t.tenant_id, report_id)
    row = st["row"]
    assert row.status == "completed" and row.sealed_at is not None
    assert st["types"] == ["report_started", "report_generated"], st["types"]
    assert st["audits"] == ["audit.report_completed"]
    assert st["evidence"] == {"complete": len(st["files"])}
    names = [f.name for f in st["files"]]
    assert names == ["units.jsonl", "observations.jsonl", "renders.jsonl", "report.json"]
    generated = st["events"][-1].payload
    records = [{k: getattr(f, k) for k in ("ord", "name", "media_type", "sha256", "size", "rows",
                                           "version_id")} for f in st["files"]]  # fmt: skip
    assert generated["files"] == records and generated["files_root"] == files_root(records)
    assert generated["files_root"] == row.files_root
    verified = await verify_chain(
        sessions, s3, settings, tenant_id=t.tenant_id, stream_id=report_id, require_seal=True
    )
    assert verified.ok, verified.errors
    stored = await stored_bytes(s3, settings, t, report_id, st["files"])
    want = await expected_files(
        sessions, s3, settings, t, job_id, dict(row.snapshot),
        {"renderer_version": row.renderer_version, "toolchain_id": row.toolchain_id,
         "unicode_version": row.unicode_version, "paper": row.paper},
        row.image_digest,
    )  # fmt: skip
    if stored != want:  # name the differing section (a mutable input outside the snapshot)
        a, b = json.loads(stored["report.json"]), json.loads(want["report.json"])
        diff = {k: (a.get(k), b.get(k)) for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
        raise AssertionError(f"stored != rebuilt; report.json differs in {diff}")
    assert json.loads(stored["report.json"])["job"]["clean"] is row.clean
    for f in st["files"]:  # locked like evidence
        lock = await s3.get_object_retention(
            Bucket=settings.s3_evidence_bucket, Key=f"t/{t.tenant_id}/reports/{report_id}/{f.name}",
            VersionId=f.version_id,
        )  # fmt: skip
        assert lock["Retention"]["Mode"] == "COMPLIANCE"
    return st
