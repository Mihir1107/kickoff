"""Checks every corpus render against its oracle, the structural EML checks, a golden (a regression
guard only) and ``edisc-verify`` on the exported render package."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector
from edisc_core.canonical import canonical_json
from edisc_core.schemas import ARCHIVE_CAVEAT
from edisc_core.settings import Settings
from edisc_custody.render_export import export_render_package
from edisc_custody.render_package import verify_render_package
from edisc_db.session import tenant_tx
from edisc_renderers.rsmf import golden_key

from ...unit.renderers.emlcheck import check_eml, custom
from .cases import Case
from .oracle import RenderOracle, ts_day

Sessions = async_sessionmaker[AsyncSession]
GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "rsmf-corpus"
# values derived from the tenant or job ids (random per run): masked in the golden projection
MASKED_CUSTOM = {
    "edisc.idempotency_key",
    "edisc.prior_version_keys",
    "edisc.reactions.idempotency_key",
}


async def stored(
    sessions: Sessions, s3: S3Client, settings: Settings, tenant_id: uuid.UUID, render_id: uuid.UUID
) -> list[tuple[dict[str, Any], bytes]]:
    async with tenant_tx(sessions, tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT rf.record, e.storage_key, e.version_id FROM render_files rf"
                    " JOIN evidence_objects e ON e.id = rf.evidence_object_id"
                    " WHERE rf.render_id = :r ORDER BY rf.ord"
                ),
                {"r": render_id},
            )
        ).all()
    out = []
    for r in rows:
        resp = await s3.get_object(
            Bucket=settings.s3_evidence_bucket, Key=r.storage_key, VersionId=r.version_id
        )
        async with resp["Body"] as body:
            out.append((dict(r.record), await body.read()))
    return out


def check_against_oracle(
    case: Case,
    oracle: RenderOracle,
    files: list[tuple[dict[str, Any], bytes]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Every assertion is against the oracle (computed from the dataset alone). Returns the parsed
    manifests by file name, for the golden projection."""
    manifests: dict[str, Any] = {}
    primaries: Counter[str] = Counter()
    context: set[str] = set()
    cap = case.options.cap
    if case.expect_files is not None:
        assert len(files) == case.expect_files, [r["name"] for r, _ in files]
    assert {(r["conversation_id"], r["day"]) for r, _ in files} == {
        (c, d.isoformat()) for c, d in oracle.slices
    }
    for record, data in files:
        parsed = check_eml(data)  # structure, schema, headers consistent with the manifest
        manifests[record["name"]] = parsed.manifest
        events = parsed.manifest["events"]
        assert len(events) <= cap
        if case.source == "export":
            assert parsed.headers["X-RSMF-CompletenessBasis"] == "archive"
            assert ARCHIVE_CAVEAT in parsed.text
        (conv,) = parsed.manifest["conversations"]
        for e in events:
            c = custom(e)
            subject = c["edisc.source_item_id"][0]
            if "edisc.context" in c:
                context.add(subject)
                continue
            primaries[subject] += 1
            want = oracle.primaries.get(subject)
            assert want is not None, f"{subject}: rendered but not in scope"
            where = f"{record['name']}: {subject}"
            assert ts_day(want.ts, case.options.time_zone).isoformat() == record["day"], where
            assert e["type"] == want.type, (where, e["type"], want.type)
            assert bool(e.get("deleted")) == want.deleted, where
            assert len(e.get("edits", [])) == want.edits, (where, e.get("edits"), want.edits)
            if want.deleted:  # reactions recorded before the deletion: history only (custom)
                assert "reactions" not in e, where
                got_reactions = {
                    v.split(" (", 1)[0] for v in c.get("edisc.reactions_before_deletion", [])
                }
            else:
                assert "edisc.reactions_before_deletion" not in c, where
                got_reactions = {r["value"] for r in e.get("reactions", [])}
            assert got_reactions == set(want.reactions), (where, got_reactions, set(want.reactions))
            ids = {a["id"] for a in e.get("attachments", [])}
            held = {i for i in ids if not i.endswith(".UNAVAILABLE.txt")}
            assert {i.split("_", 1)[0] for i in held} == set(want.files), where
            assert {i.split("_", 1)[0] for i in ids - held} == set(want.unavailable), where
            if want.subtype in ("thread_broadcast", "reply_broadcast"):
                assert c.get("slack.subtype") == [want.subtype], where
            if want.root is None or not case.check_threads:
                continue
            if (case.options.include_context and want.root_linked) or want.root_primary_same_slice:
                assert e.get("parent") == want.root, where
            else:
                assert "parent" not in e, where
                reason = "context_excluded" if want.root_linked else "not_collected"
                assert c["edisc.parent_not_rendered"] == [want.root], where
                assert c["edisc.parent_not_rendered_reason"] == [reason], where
        assert conv.get("custom")
    assert primaries == Counter(dict.fromkeys(oracle.primaries, 1)), "every in-scope message once"
    if case.check_threads:
        assert context == oracle.context_roots
    assert summary["items_in"] == summary["events_out"] == len(oracle.primaries)
    return manifests


def _masked(manifest: dict[str, Any]) -> dict[str, Any]:
    m = json.loads(json.dumps(manifest))
    m.pop("eventcollectionid", None)
    for e in m["events"]:
        e["custom"] = [p for p in e.get("custom", []) if p["name"] not in MASKED_CUSTOM]
    return m


def check_golden(name: str, manifests: dict[str, Any]) -> None:
    """A regression guard keyed by the renderer AND the dummy version (a bump never rewrites one):
    the masked manifests (ids derived from the random tenant and job removed) hash to the recorded
    value. Recorded only with EDISC_RECORD_CORPUS=1, never in CI, never over an existing file."""
    projection = canonical_json({k: _masked(v) for k, v in sorted(manifests.items())})
    doc = {
        "projection_sha256": hashlib.sha256(projection).hexdigest(),
        "files": sorted(manifests),
        "events": sum(len(m["events"]) for m in manifests.values()),
    }
    path = GOLDEN / f"{golden_key()}_dummy-{DummyConnector.version}" / f"{name}.json"
    if not path.exists():
        if os.environ.get("EDISC_RECORD_CORPUS") != "1":
            pytest.fail(
                f"no corpus golden {path.relative_to(GOLDEN)}; record with EDISC_RECORD_CORPUS=1"
            )
        if os.environ.get("CI"):
            pytest.fail("corpus goldens are never recorded in CI")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        return
    assert json.loads(path.read_text()) == doc, (
        f"corpus output changed for {name} (a regression guard)"
    )


async def check_package(
    sessions: Sessions, s3: S3Client, settings: Settings, tenant_id: uuid.UUID,
    render_id: uuid.UUID, dest: Path,
) -> None:  # fmt: skip
    pkg = await export_render_package(
        sessions, s3, settings, tenant_id=tenant_id, render_id=render_id, dest=dest
    )
    report = verify_render_package(pkg)
    assert report.ok, (report.errors, report.chain.errors if report.chain else None)
