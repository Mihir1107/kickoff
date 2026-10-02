"""Slack export upload, hash-and-lock and validation (ADR 0014 sections 1, 3, 5; R1, R2, R7), through the
real API, a real Temporal worker on the ``exports`` queue, Postgres and MinIO (nothing mocked)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import struct
import uuid
import zipfile
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from temporalio.worker import Worker

from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_worker.contracts import EXPORTS_QUEUE
from edisc_worker.exports import ExportActivities, ExportIngest
from edisc_worker.workflows import ExportIngestWorkflow

from ...unit.custody.zips import cd_offset, make_zip, patch_cd
from .conftest import Api, TenantCtx, add_principal

MiB = 1 << 20


def export_settings(api: Api, **overrides: Any) -> Settings:
    return api.settings.model_copy(
        update={
            "export_upload_part_min_bytes": 5 * MiB,
            "export_complete_wait_seconds": 20,
            "export_read_window_bytes": 1 * MiB,
            **overrides,
        }
    )


@pytest.fixture
async def exp(api: Api) -> AsyncIterator[Api]:
    """The API with export settings, and the ``exports`` worker those settings drive."""
    settings = export_settings(api, export_entry_batch=3)  # several batches even for small zips
    api.resources.settings = settings
    tuned = Api(settings, api.sessions, api.s3, api.temporal, api.resources, api.http)
    acts = ExportActivities(api.sessions, api.s3, settings)
    async with Worker(
        api.temporal,
        task_queue=EXPORTS_QUEUE,
        workflows=[ExportIngestWorkflow],
        activities=acts.all(),
    ):
        yield tuned


def slack_export(*, full: bool = False, extra: dict[str, bytes] | None = None) -> bytes:
    channels = [{"id": "C1", "name": "general"}, {"id": "C2", "name": "random"},
                {"id": "C3", "name": "quiet"}]  # fmt: skip
    files: dict[str, bytes] = {
        "users.json": json.dumps([{"id": "U1", "name": "alice"}]).encode(),
        "channels.json": json.dumps(channels).encode(),
        "general/": b"",
        "general/2026-01-05.json": json.dumps([{"ts": "1767571200.000100", "text": "hi"}]).encode(),
        "general/2026-01-06.json": json.dumps([{"ts": "1767657600.000100", "text": "yo"}]).encode(),
        "random/2026-01-05.json": b"[]",
        "orphan/2026-01-05.json": b"[]",  # a folder no metadata file lists
        "notes.txt": b"not part of the layout",  # unknown entry (R2)
    }
    if full:
        files["dms.json"] = json.dumps([{"id": "D1", "members": ["U1", "U2"]}]).encode()
        files["D1/2026-01-05.json"] = b"[]"
    files.update(extra or {})
    return make_zip(files)


def digest_header(data: bytes) -> dict[str, str]:
    return {
        "content-digest": f"sha-256=:{base64.b64encode(hashlib.sha256(data).digest()).decode()}:"
    }


async def create(
    c: httpx.AsyncClient, client_id: uuid.UUID, size: int, **body: Any
) -> httpx.Response:
    return await c.post(f"/v1/clients/{client_id}/exports", json={"size_bytes": size, **body})


async def put_part(c: httpx.AsyncClient, export_id: str, n: int, data: bytes) -> httpx.Response:
    return await c.put(
        f"/v1/exports/{export_id}/parts/{n}", content=data, headers=digest_header(data)
    )


async def upload(
    c: httpx.AsyncClient,
    client_id: uuid.UUID,
    data: bytes,
    *,
    part_size: int | None = None,
    **body: Any,
) -> tuple[str, httpx.Response]:
    """Create, send the parts, complete. Returns the export id and the completion response."""
    r = await create(c, client_id, len(data), **body)
    assert r.status_code == 201, r.text
    export_id = r.json()["id"]
    step = part_size or len(data)
    for n, start in enumerate(range(0, len(data), step), start=1):
        r = await put_part(c, export_id, n, data[start : start + step])
        assert r.status_code == 200, r.text
    return export_id, await c.post(f"/v1/exports/{export_id}/complete")


async def settled(c: httpx.AsyncClient, export_id: str) -> dict[str, Any]:
    async with asyncio.timeout(60):
        while True:
            out: dict[str, Any] = (await c.get(f"/v1/exports/{export_id}")).json()
            if out["status"] in ("ready", "rejected"):
                return out
            await asyncio.sleep(0.2)


async def audit_events(
    api: Api, t: TenantCtx, prefix: str = "audit.export"
) -> list[tuple[str, str, dict[str, Any]]]:
    async with tenant_tx(api.sessions, t.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT event_type, actor, payload FROM custody_events WHERE stream_id = :t"
                    " AND event_type LIKE :p ORDER BY seq"
                ),
                {"t": t.tenant_id, "p": f"{prefix}%"},
            )
        ).all()
    return [(r.event_type, r.actor, r.payload) for r in rows]


# ------------------------------------------------------------------ the happy path
async def test_upload_is_locked_audited_validated_and_becomes_a_connection(
    exp: Api, tenant: TenantCtx
) -> None:
    data = slack_export()
    sha = hashlib.sha256(data).hexdigest()
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id, done = await upload(c, tenant.default_client_id, data, sha256=sha, plan="pro")
        assert done.status_code == 202, done.text
        out = await settled(c, export_id)
        conn = (await c.get(f"/v1/connections/{out['connection_id']}")).json()

    assert out["status"] == "ready", out
    assert (out["sha256"], out["size_bytes"]) == (sha, len(data))
    assert (out["detected_tier"], out["tier_confirmed"]) == ("public_only", True)
    f = out["findings"]
    assert f["unknown_entries"] == {"count": 1, "sample": ["notes.txt"]}  # listed, not dropped (R2)
    assert f["folders_without_conversation"] == {"count": 1, "sample": ["orphan"]}
    assert f["conversations_without_messages"] == {"count": 1, "sample": ["C3"]}
    assert f["entries_by_kind"] == {"metadata": 2, "directory": 1, "day": 4, "unknown": 1}
    assert f["conversations"] == 3 and f["metadata_files"] == ["channels.json", "users.json"]
    assert any("not in this export" in b for b in f["blind_spots"])
    assert out["entry_count"] == 8
    assert (conn["source"], conn["plan_tier"], conn["status"]) == (
        "slack_export",
        "public_only",
        "active",
    )
    assert conn["client_id"] == str(tenant.default_client_id)

    # the archive is locked evidence (pinned version, COMPLIANCE) and staging is gone
    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        ev = (
            await s.execute(
                text(
                    "SELECT storage_key, state, sha256, version_id, kind FROM evidence_objects WHERE id = :i"
                ),
                {"i": uuid.UUID(out["evidence_object_id"])},
            )
        ).one()
        entries = (
            await s.execute(
                text(
                    "SELECT name, kind, folder, hint_day FROM export_entries WHERE export_id = :e ORDER BY idx"
                ),
                {"e": uuid.UUID(export_id)},
            )
        ).all()
        cfg = (
            await s.execute(
                text("SELECT config FROM connections WHERE id = :i"),
                {"i": uuid.UUID(out["connection_id"])},
            )
        ).scalar_one()
    assert (ev.state, ev.sha256, ev.version_id, ev.kind) == (
        "complete",
        sha,
        out["version_id"],
        "file",
    )
    retention = await exp.s3.get_object_retention(
        Bucket=exp.settings.s3_evidence_bucket, Key=ev.storage_key, VersionId=ev.version_id
    )
    assert retention["Retention"]["Mode"] == "COMPLIANCE"
    with pytest.raises(ClientError):
        await exp.s3.head_object(
            Bucket=exp.settings.s3_staging_bucket, Key=f"exports/{tenant.tenant_id}/{export_id}"
        )
    assert len(entries) == 8 and tuple(entries[3][:3]) == (
        "general/2026-01-05.json",
        "day",
        "general",
    )
    assert cfg == {"export_id": export_id}

    events = await audit_events(exp, tenant)
    assert [e[0] for e in events] == [
        "audit.export_upload_started",
        "audit.export_upload_completed",
        "audit.export_uploaded",
        "audit.export_validated",
    ]
    assert events[0][1] == events[1][1] == f"user:{tenant.admin_id}"  # the acting user
    uploaded = events[2][2]
    assert uploaded["uploaded_by"] == f"user:{tenant.admin_id}"
    assert (uploaded["sha256"], uploaded["size_bytes"], uploaded["version_id"]) == (
        sha,
        len(data),
        out["version_id"],
    )


async def test_full_export_with_plan_mismatch_is_flagged(exp: Api, tenant: TenantCtx) -> None:
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id, _ = await upload(
            c, tenant.default_client_id, slack_export(full=True), plan="pro"
        )
        out = await settled(c, export_id)
    assert (out["status"], out["detected_tier"]) == ("ready", "full")
    assert any("Declared plan pro" in w for w in out["findings"]["tier_warnings"])
    assert not any("not in this export" in b for b in out["findings"]["blind_spots"])


# ------------------------------------------------------------------ R7: declared hash
async def test_declared_hash_mismatch_rejects_before_parsing_but_keeps_the_evidence(
    exp: Api, tenant: TenantCtx
) -> None:
    data = slack_export()
    wrong = hashlib.sha256(b"something else").hexdigest()
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id, done = await upload(c, tenant.default_client_id, data, sha256=wrong)
        assert done.status_code == 422, done.text
        body = done.json()
        assert body["error"] == "declared_hash_mismatch"
        assert (body["declared_sha256"], body["sha256"]) == (
            wrong,
            hashlib.sha256(data).hexdigest(),
        )
        out = await settled(c, export_id)
        again = await c.post(
            f"/v1/exports/{export_id}/complete"
        )  # the same clear error, every time
    assert again.status_code == 422 and again.json()["error"] == "declared_hash_mismatch"
    assert (out["status"], out["reject_reason"]) == ("rejected", "declared_hash_mismatch")
    assert out["evidence_object_id"] and out["version_id"]  # locked as received
    assert out["connection_id"] is None and out["entry_count"] is None
    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        parsed = (
            await s.execute(
                text("SELECT count(*) FROM export_entries WHERE export_id = :e"),
                {"e": uuid.UUID(export_id)},
            )
        ).scalar_one()
    assert parsed == 0  # nothing read the archive
    types = [e[0] for e in await audit_events(exp, tenant)]
    assert types[-2:] == ["audit.export_uploaded", "audit.export_rejected"]


# ------------------------------------------------------------------ parts
async def test_multipart_upload_with_retries_digest_checks_and_part_rules(
    exp: Api, tenant: TenantCtx
) -> None:
    filler = os.urandom(6 * MiB)  # incompressible, stored: makes the zip span two parts
    data = make_zip(
        {"channels.json": b"[]", "big/2026-01-05.json": filler}, method=zipfile.ZIP_STORED
    )
    first, second = data[: 5 * MiB + 17], data[5 * MiB + 17 :]
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id = (await create(c, tenant.default_client_id, len(data))).json()["id"]
        url = f"/v1/exports/{export_id}/parts"
        r = await c.put(f"{url}/1", content=first)
        assert r.status_code == 400 and r.json()["error"] == "missing_digest"
        r = await c.put(f"{url}/1", content=first, headers=digest_header(b"other"))
        assert r.status_code == 422 and r.json()["error"] == "digest_mismatch"
        r = await c.put(f"{url}/2", content=second, headers=digest_header(second))
        assert r.status_code == 200
        r = await c.post(f"/v1/exports/{export_id}/complete")
        assert r.status_code == 422 and "missing [1]" in r.json()["detail"]
        for _ in range(2):  # a re-sent part replaces the earlier one
            r = await put_part(c, export_id, 1, first)
            assert r.status_code == 200 and r.json()["sha256"] == hashlib.sha256(first).hexdigest()
        r = await put_part(c, export_id, 3, b"x")  # would exceed the declared size
        assert r.status_code == 422
        status = (await c.get(f"/v1/exports/{export_id}")).json()
        assert status["upload"]["parts_received"] == 2
        assert status["upload"]["bytes_received"] == len(data)
        r = await c.post(f"/v1/exports/{export_id}/complete")
        assert r.status_code == 202, r.text
        out = await settled(c, export_id)
        late = await put_part(c, export_id, 1, first)
    assert late.status_code == 409
    assert (out["status"], out["sha256"]) == ("ready", hashlib.sha256(data).hexdigest())


async def test_parts_below_the_minimum_and_wrong_totals_are_refused(
    exp: Api, tenant: TenantCtx
) -> None:
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id = (await create(c, tenant.default_client_id, 300)).json()["id"]
        assert (await put_part(c, export_id, 1, b"a" * 100)).status_code == 200
        assert (await put_part(c, export_id, 2, b"b" * 100)).status_code == 200
        r = await c.post(f"/v1/exports/{export_id}/complete")
        assert r.status_code == 422 and "too small: [1]" in r.json()["detail"]
        export2 = (await create(c, tenant.default_client_id, 300)).json()["id"]
        assert (await put_part(c, export2, 1, b"a" * 100)).status_code == 200
        r = await c.post(f"/v1/exports/{export2}/complete")
        assert r.status_code == 422 and "received 100 bytes, declared 300" in r.json()["detail"]
        assert (await c.get(f"/v1/exports/{export2}")).json()["status"] == "uploading"


async def test_size_limit_and_permissions(exp: Api, tenant: TenantCtx) -> None:
    _, reviewer = await add_principal(
        exp, tenant, roles=[("reviewer", "client", tenant.default_client_id)]
    )
    _, client_admin = await add_principal(
        exp, tenant, roles=[("client_admin", "client", tenant.default_client_id)]
    )
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as admin:
        r = await create(admin, tenant.default_client_id, exp.settings.export_max_archive_bytes + 1)
        assert r.status_code == 413
        export_id = (await create(admin, tenant.default_client_id, 10)).json()["id"]
    async with exp.client(tenant.subdomain, tenant.token(exp.settings, subject=reviewer)) as c:
        assert (await create(c, tenant.default_client_id, 10)).status_code == 403
        assert (await put_part(c, export_id, 1, b"x")).status_code == 403
        assert (await c.post(f"/v1/exports/{export_id}/complete")).status_code == 403
    async with exp.client(tenant.subdomain, tenant.token(exp.settings, subject=client_admin)) as c:
        assert (await create(c, tenant.default_client_id, 10)).status_code == 201
        # R1: only a tenant admin may override limits
        r = await create(c, tenant.default_client_id, 10, limits={"max_entries": 5})
        assert r.status_code == 403
        assert (await c.get(f"/v1/clients/{tenant.default_client_id}/exports")).json()["items"]


# ------------------------------------------------------------------ R1: audited limit overrides
async def test_tenant_admin_overrides_are_audited_and_enforced(exp: Api, tenant: TenantCtx) -> None:
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id, _ = await upload(
            c,
            tenant.default_client_id,
            slack_export(),
            limits={"max_entries": 5, "max_name_bytes": 64},
        )
        out = await settled(c, export_id)
    assert out["limits"]["max_entries"] == 5 and out["limits"]["max_name_bytes"] == 64
    assert out["limits"]["max_entry_bytes"] == exp.settings.export_max_entry_bytes
    assert (out["status"], out["reject_reason"], out["reject_detail"]["code"]) == (
        "rejected", "archive_invalid", "too_many_entries",
    )  # fmt: skip
    overridden = [
        e for e in await audit_events(exp, tenant) if e[0] == "audit.export_limits_overridden"
    ]
    assert len(overridden) == 1 and overridden[0][1] == f"user:{tenant.admin_id}"
    assert overridden[0][2]["overrides"] == {
        "max_entries": {"default": 20_000_000, "override": 5},
        "max_name_bytes": {"default": 1024, "override": 64},
    }


# ------------------------------------------------------------------ hostile and broken archives
def _bomb() -> bytes:
    return slack_export(extra={"general/2026-01-07.json": b" " * (4 * MiB)})


def _duplicate_across_batches() -> bytes:
    # entry_batch = 3: the case-folded twin lands in a later batch, so the database catches it
    return slack_export(extra={"General/2026-01-05.json": b"[]"})


def _overlapping() -> bytes:
    """Two day files whose directory records point at the same local header (the classic overlapping
    bomb). Day files are not read during validation, so only the directory-level check can catch it."""
    data = slack_export()
    names = list(zipfile.ZipFile(io.BytesIO(data)).namelist())
    a, b = names.index("general/2026-01-05.json"), names.index("general/2026-01-06.json")
    pos = cd_offset(data)
    offsets = []
    for _ in names:
        nlen, xlen, clen = struct.unpack_from("<HHH", data, pos + 28)
        offsets.append(struct.unpack_from("<I", data, pos + 42)[0])
        pos += 46 + nlen + xlen + clen
    return patch_cd(data, b, "<I", 42, offsets[a])


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (_bomb, "compression_ratio"),
        (_duplicate_across_batches, "duplicate_name"),
        (_overlapping, "overlap"),
        (
            lambda: make_zip({"users.json": b"[]", "general/2026-01-05.json": b"[]"}),
            "not_a_slack_export",
        ),
        (
            lambda: slack_export(
                extra={"channels.json": b'[{"id": "C1", "name": "general"}, oops]'}
            ),
            "metadata_invalid",
        ),
        (lambda: slack_export(extra={"channels.json": b'{"not": "an array"}'}), "metadata_invalid"),
        (lambda: slack_export()[:-30], "bad_eocd"),
    ],
)
async def test_hostile_or_broken_archives_are_rejected_with_a_classified_finding(
    exp: Api, tenant: TenantCtx, build: Any, code: str
) -> None:
    data = build()
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id, done = await upload(c, tenant.default_client_id, data)
        assert done.status_code == 202
        out = await settled(c, export_id)
    assert (out["status"], out["reject_reason"]) == ("rejected", "archive_invalid"), out
    got = out["reject_detail"]["code"]
    if code == "bad_eocd":
        assert got in ("bad_eocd", "bad_central_directory", "truncated", "out_of_bounds")
    else:
        assert got == code, out["reject_detail"]
    assert out["evidence_object_id"]  # rejected archives stay locked evidence
    assert out["connection_id"] is None
    rejected = [e for e in await audit_events(exp, tenant) if e[0] == "audit.export_rejected"]
    assert rejected and rejected[-1][2]["code"] == got


# ------------------------------------------------------------------ idempotency and resume
async def test_lock_and_validate_are_idempotent_and_resume_partial_work(
    exp: Api, tenant: TenantCtx
) -> None:
    ingest = ExportIngest(exp.sessions, exp.s3, exp.settings)
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id, _ = await upload(c, tenant.default_client_id, slack_export())
        out = await settled(c, export_id)
    eid = uuid.UUID(export_id)
    before = await audit_events(exp, tenant)
    assert await ingest.lock(tenant.tenant_id, eid) == {"status": "ready"}
    assert await ingest.validate(tenant.tenant_id, eid) == {"status": "ready"}
    assert await audit_events(exp, tenant) == before
    assert out["status"] == "ready"


async def test_validation_resumes_after_a_crash_between_entry_batches(
    exp: Api, tenant: TenantCtx
) -> None:
    """A validation attempt that died after writing some batches: the retry writes the rest once."""
    data = slack_export()
    ingest = ExportIngest(exp.sessions, exp.s3, exp.settings)
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        r = await create(c, tenant.default_client_id, len(data))
        export_id = r.json()["id"]
        await put_part(c, export_id, 1, data)
    eid = uuid.UUID(export_id)
    async with tenant_tx(
        exp.sessions, tenant.tenant_id
    ) as s:  # what complete does, without the workflow
        await s.execute(
            text("UPDATE slack_exports SET status = 'locking' WHERE id = :i"), {"i": eid}
        )
    assert await ingest.lock(tenant.tenant_id, eid) == {"status": "validating"}

    crashed = ExportIngest(exp.sessions, exp.s3, exp.settings)
    calls = 0
    original = crashed._insert_entries

    async def die_after_two(t: uuid.UUID, e: uuid.UUID, batch: list[dict[str, Any]]) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ConnectionError("simulated crash")
        await original(t, e, batch)

    crashed._insert_entries = die_after_two  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await crashed.validate(tenant.tenant_id, eid)
    result = await ingest.validate(tenant.tenant_id, eid)
    assert result["status"] == "ready"
    async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
        n = (
            await s.execute(
                text("SELECT count(*) FROM export_entries WHERE export_id = :e"), {"e": eid}
            )
        ).scalar_one()
    assert n == 8


async def test_a_part_changed_in_the_store_after_it_was_recorded_blocks_completion(
    exp: Api, tenant: TenantCtx
) -> None:
    """A concurrent re-send with a different body can leave the store and our records disagreeing;
    completion refuses until the part is sent again."""
    data = slack_export()
    async with exp.client(tenant.subdomain, tenant.token(exp.settings)) as c:
        export_id = (await create(c, tenant.default_client_id, len(data))).json()["id"]
        assert (await put_part(c, export_id, 1, data)).status_code == 200
        async with tenant_tx(exp.sessions, tenant.tenant_id) as s:
            upload_id = (
                await s.execute(
                    text("SELECT upload_id FROM slack_exports WHERE id = :i"),
                    {"i": uuid.UUID(export_id)},
                )
            ).scalar_one()
        other = bytes(reversed(data))
        await exp.s3.upload_part(
            Bucket=exp.settings.s3_staging_bucket, Key=f"exports/{tenant.tenant_id}/{export_id}",
            UploadId=upload_id, PartNumber=1, Body=other,
            ChecksumSHA256=base64.b64encode(hashlib.sha256(other).digest()).decode(),
        )  # fmt: skip
        r = await c.post(f"/v1/exports/{export_id}/complete")
        assert r.status_code == 409 and "part 1 changed" in r.json()["detail"]
        assert (await put_part(c, export_id, 1, data)).status_code == 200
        assert (await c.post(f"/v1/exports/{export_id}/complete")).status_code == 202
        out = await settled(c, export_id)
    assert (out["status"], out["sha256"]) == ("ready", hashlib.sha256(data).hexdigest())
