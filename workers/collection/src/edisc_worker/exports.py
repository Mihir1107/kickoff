"""Slack export ingestion before any job (ADR 0014 sections 1, 3 and 5): hash and lock, then validate.

``lock``: the staged upload is completed, streamed once for OUR SHA-256 and size, and locked as evidence
(content-addressed, verified after the copy). ``audit.export_uploaded`` is committed with the result
before anything parses the archive. A declared SHA-256 that differs from ours rejects the export right
there (R7): it stays locked, the mismatch is audited, nothing parses it.

``validate``: one streaming pass over the central directory of the LOCKED version (bounded memory, R1).
Entries are classified and written in batches; the database detects duplicate names. Then the
conversation metadata files are streamed element by element, the tier is detected, and the export
becomes ``ready`` with a credential-less ``slack_export`` connection, or ``rejected`` with a classified
finding. Both steps are idempotent: a retry after a crash anywhere redoes or skips work, never doubles it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError
from types_aiobotocore_s3 import S3Client

from edisc_connector_slack_export.archive_access import archive_limits, entry_from_row
from edisc_connector_slack_export.layout import (
    CONVERSATION_FILES,
    KNOWN_METADATA,
    Placement,
    RootDetector,
    Tier,
    classify,
    conversation_record,
    detect_tier,
    is_junk,
    nested_metadata,
    relative,
)
from edisc_core.ids import new_id
from edisc_core.jsonstream import JsonStreamError, iter_array_elements
from edisc_core.logs import get_logger
from edisc_core.settings import Settings
from edisc_custody.archive import (
    ArchiveError,
    ArchiveErrorCode,
    ArchiveLimits,
    Entry,
    NameEncoding,
    fold_name,
    iter_central_directory,
    locate_directory,
    open_entry,
)
from edisc_custody.log import anchor_if_due, append
from edisc_db.session import tenant_tx
from edisc_evidence.archive_source import CoalescingSource, S3ObjectSource
from edisc_evidence.writer import EvidenceWriter
from edisc_normalizer.slack import ts_datetime
from edisc_worker.activities import _ticking
from edisc_worker.contracts import ExportRef
from edisc_worker.pipeline import CrashHooks

log = get_logger(__name__)

ACTOR = "system:export-ingest"
SAMPLE = 100  # names listed per finding; the full lists stay queryable in export_entries


def _duplicate(first: str, second: str) -> ArchiveError:
    return ArchiveError(ArchiveErrorCode.DUPLICATE_NAME, f"{first!r} and {second!r}")


class ExportRejectedError(Exception):
    """The archive is not acceptable: recorded as a finding, the export becomes ``rejected``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass
class _Scan:
    entries: int = 0
    kinds: Counter[str] = field(default_factory=Counter)
    metadata: dict[str, Entry] = field(default_factory=dict)  # known metadata files only (bounded)
    unknown: list[str] = field(default_factory=list)
    unrecognised_metadata: list[str] = field(default_factory=list)
    unrecognised_metadata_count: int = 0
    nested_metadata: bool = False
    root: str | None = None
    junk: int = 0
    junk_sample: list[str] = field(default_factory=list)
    encodings: Counter[str] = field(default_factory=Counter)
    encoding_samples: dict[str, list[str]] = field(default_factory=dict)
    workspace: str | None = None
    messages: int = 0
    threaded: int = 0
    anomalies: int = 0
    anomaly_sample: list[str] = field(default_factory=list)
    elements_without_ts: int = 0
    unparseable: int = 0
    unparseable_sample: list[str] = field(default_factory=list)


class ExportIngest:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        s3: S3Client,
        settings: Settings,
        hooks: CrashHooks | None = None,
    ) -> None:
        self.sessions, self.s3, self.settings = sessions, s3, settings
        self.hooks = hooks or CrashHooks()  # crash-matrix seam (M14.7); no-op in production

    # ------------------------------------------------------------------ hash and lock
    async def lock(self, tenant_id: uuid.UUID, export_id: uuid.UUID) -> dict[str, Any]:
        row = await self._row(tenant_id, export_id)
        staging = self.settings.s3_staging_bucket
        if row.status != "locking":
            if row.status in ("validating", "ready", "rejected"):  # crash after the commit below
                await self.s3.delete_object(Bucket=staging, Key=row.staging_key)
            return {"status": row.status}
        problem = await self._complete_staged_upload(tenant_id, export_id, row)
        if problem is not None:
            return await self._reopen(tenant_id, export_id, problem)
        await self.hooks.hit("lock:after_complete")
        sha256, size = await self._hash_staged(row.staging_key)
        await self.hooks.hit("lock:after_hash")
        if size != row.declared_size:  # the parts were checked against it: storage misbehaved
            raise ApplicationError(
                f"staged export is {size} bytes, declared {row.declared_size}",
                type="StagedSizeMismatch",
                non_retryable=True,
            )
        written = await EvidenceWriter(self.sessions, self.s3, self.settings).lock_staged(
            tenant_id=tenant_id, staging_key=row.staging_key, sha256=sha256, size=size
        )
        await self.hooks.hit("lock:after_evidence")
        mismatch = row.declared_sha256 is not None and row.declared_sha256 != sha256
        detail = {"declared_sha256": row.declared_sha256, "sha256": sha256} if mismatch else None
        async with tenant_tx(self.sessions, tenant_id) as s:
            done = await s.execute(
                text(
                    "UPDATE slack_exports SET status = :st, reject_reason = :rr,"
                    " reject_detail = CAST(:rd AS jsonb), sha256 = :h, size_bytes = :n,"
                    " evidence_object_id = :ev, version_id = :v, locked_at = now(), updated_at = now()"
                    " WHERE id = :i AND status = 'locking'"
                ),
                {"st": "rejected" if mismatch else "validating",
                 "rr": "declared_hash_mismatch" if mismatch else None,
                 "rd": json.dumps(detail) if detail else None, "h": sha256, "n": size,
                 "ev": written.evidence_id, "v": written.version_id, "i": export_id},
            )  # fmt: skip
            if done.rowcount == 1:  # type: ignore[attr-defined]
                await self._audit(
                    s, tenant_id, "export_uploaded",
                    {"export_id": str(export_id), "client_id": str(row.client_id),
                     "uploaded_by": row.created_by, "size_bytes": size, "sha256": sha256,
                     "evidence_object_id": str(written.evidence_id), "version_id": written.version_id,
                     "deduplicated": written.deduplicated, "declared_sha256": row.declared_sha256,
                     "declared_plan": row.declared_plan},
                )  # fmt: skip
                if mismatch:
                    await self._audit(
                        s, tenant_id, "export_rejected",
                        {"export_id": str(export_id), "reason": "declared_hash_mismatch", **(detail or {})},
                    )  # fmt: skip
        await self.hooks.hit("lock:after_commit")
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=tenant_id
        )
        await self.s3.delete_object(Bucket=staging, Key=row.staging_key)
        status = (await self._row(tenant_id, export_id)).status
        log.info("export locked", export_id=str(export_id), sha256=sha256, status=status)
        return {"status": status}

    async def _complete_staged_upload(
        self, tenant_id: uuid.UUID, export_id: uuid.UUID, row: Any
    ) -> str | None:
        """Complete the multipart upload unless a previous attempt already did. Returns a problem code
        when the store refuses: a part changed after it was recorded (a client re-sent a different body
        concurrently) or the upload expired. Nothing was locked yet, so the export is reopened."""
        staging = self.settings.s3_staging_bucket
        try:
            await self.s3.head_object(Bucket=staging, Key=row.staging_key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) not in ("404", "NoSuchKey"):
                raise
        else:
            return None
        async with tenant_tx(self.sessions, tenant_id) as s:
            parts = (
                await s.execute(
                    text(
                        "SELECT part_number, etag, sha256 FROM export_upload_parts"
                        " WHERE export_id = :i ORDER BY part_number"
                    ),
                    {"i": export_id},
                )
            ).all()
        try:
            await self.s3.complete_multipart_upload(
                Bucket=staging,
                Key=row.staging_key,
                UploadId=row.upload_id,
                MultipartUpload={
                    "Parts": [
                        {
                            "PartNumber": p.part_number,
                            "ETag": p.etag,
                            "ChecksumSHA256": base64.b64encode(bytes.fromhex(p.sha256)).decode(),
                        }
                        for p in parts
                    ]
                },
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code"))
            if code in ("InvalidPart", "InvalidPartOrder", "BadDigest", "InvalidRequest"):
                return "parts_changed"
            if code == "NoSuchUpload":
                return "upload_expired"
            raise
        return None

    async def _reopen(
        self, tenant_id: uuid.UUID, export_id: uuid.UUID, problem: str
    ) -> dict[str, Any]:
        async with tenant_tx(self.sessions, tenant_id) as s:
            done = await s.execute(
                text(
                    "UPDATE slack_exports SET status = 'uploading', updated_at = now()"
                    " WHERE id = :i AND status = 'locking'"
                ),
                {"i": export_id},
            )
            if done.rowcount == 1:  # type: ignore[attr-defined]
                await self._audit(
                    s, tenant_id, "export_upload_reopened",
                    {"export_id": str(export_id), "reason": problem},
                )  # fmt: skip
        log.warning("export upload reopened", export_id=str(export_id), reason=problem)
        return {"status": "uploading", "error": problem}

    async def _hash_staged(self, key: str) -> tuple[str, int]:
        """One streaming pass (bounded memory): OUR hash of exactly what was uploaded."""
        resp = await self.s3.get_object(Bucket=self.settings.s3_staging_bucket, Key=key)
        h, size = hashlib.sha256(), 0
        async with resp["Body"] as body:
            async for data in body.iter_chunks(1 << 20):
                h.update(data)
                size += len(data)
        return h.hexdigest(), size

    # ------------------------------------------------------------------ validate
    async def validate(self, tenant_id: uuid.UUID, export_id: uuid.UUID) -> dict[str, Any]:
        row = await self._row(tenant_id, export_id)
        if row.status != "validating":
            return {"status": row.status}
        limits = archive_limits(row.limits)
        src = CoalescingSource(
            S3ObjectSource(
                self.s3,
                bucket=self.settings.s3_evidence_bucket,
                key=row.storage_key,
                version_id=row.version_id,
                size=row.size_bytes,
            ),
            window=self.settings.export_read_window_bytes,
        )
        try:
            scan, tier, records = await self._inspect(tenant_id, export_id, row, src, limits)
        except ArchiveError as exc:
            return await self._reject(tenant_id, export_id, exc.code.value, exc.detail)
        except ExportRejectedError as exc:
            return await self._reject(tenant_id, export_id, exc.code, exc.detail)
        findings = await self._findings(tenant_id, export_id, scan, records)
        findings.update(
            tier_warnings=list(tier.warnings),
            blind_spots=list(tier.blind_spots),
            range_requests=src.requests,
        )
        await self.hooks.hit("validate:before_ready")
        connection_id = new_id()
        async with tenant_tx(self.sessions, tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO connections (id, tenant_id, client_id, source, external_org_id,"
                    " plan_tier, granted_scopes, blind_spots, status, config) VALUES (:i, :t, :c,"
                    " 'slack_export', :org, :tier, '{}', :blind, 'active', CAST(:cfg AS jsonb))"
                ),
                {"i": connection_id, "t": tenant_id, "c": row.client_id,
                 "org": scan.workspace or f"slack-export:{export_id}", "tier": tier.tier,
                 "blind": list(tier.blind_spots),
                 "cfg": json.dumps({"export_id": str(export_id)})},
            )  # fmt: skip
            done = await s.execute(
                text(
                    "UPDATE slack_exports SET status = 'ready', entry_count = :n, detected_tier = :tier,"
                    " tier_confirmed = :conf, findings = CAST(:f AS jsonb), connection_id = :c, root_prefix = :root,"
                    " workspace_id = :ws,"
                    " validated_at = now(), updated_at = now() WHERE id = :i AND status = 'validating'"
                ),
                {"n": scan.entries, "tier": tier.tier, "conf": tier.confirmed,
                 "f": json.dumps(findings), "c": connection_id, "i": export_id, "root": scan.root,
                 "ws": scan.workspace},
            )  # fmt: skip
            if done.rowcount != 1:  # type: ignore[attr-defined]
                raise RuntimeError(f"export {export_id} left 'validating' under us")
            await self._audit(
                s, tenant_id, "export_validated",
                {"export_id": str(export_id), "connection_id": str(connection_id), "tier": tier.tier,
                 "tier_confirmed": tier.confirmed, "entry_count": scan.entries,
                 "entries_by_kind": dict(scan.kinds), "unknown_entries": findings["unknown_entries"]["count"],
                 "tier_warnings": list(tier.warnings)},
            )  # fmt: skip
        await self.hooks.hit("validate:after_ready")
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=tenant_id
        )
        log.info("export validated", export_id=str(export_id), tier=tier.tier, entries=scan.entries)
        return {"status": "ready", "connection_id": str(connection_id)}

    async def _inspect(
        self,
        tenant_id: uuid.UUID,
        export_id: uuid.UUID,
        row: Any,
        src: CoalescingSource,
        limits: ArchiveLimits,
    ) -> tuple[_Scan, Tier, Counter[str]]:
        """Everything that can reject the archive (raises ``ArchiveError`` / ``ExportRejectedError``)."""
        if row.size_bytes > limits.max_archive_bytes:
            raise ExportRejectedError(
                "archive_too_large", f"{row.size_bytes} bytes > {limits.max_archive_bytes}"
            )
        scan = await self._scan_directory(tenant_id, export_id, src, limits)
        tier = detect_tier(
            set(scan.metadata),
            nested_metadata_seen=scan.nested_metadata,
            declared_plan=row.declared_plan,
        )
        if tier is None:
            raise ExportRejectedError(
                "not_a_slack_export",
                "no channels.json, groups.json, dms.json or mpims.json at the top level",
            )
        records = await self._load_conversations(tenant_id, export_id, src, limits, scan)
        await self.hooks.hit("validate:after_conversations")
        scan.workspace = await self._workspace(src, limits, scan)
        await self._index_day_files(tenant_id, export_id, src, limits, scan)
        return scan, tier, records

    async def _workspace(
        self, src: CoalescingSource, limits: ArchiveLimits, scan: _Scan
    ) -> str | None:
        """The Slack team the export belongs to: the most common ``team_id`` in users.json (external
        members of shared channels carry other teams). Message identities are namespaced by it, as for
        the live API, so an exported message and the same message collected live are one item."""
        entry = scan.metadata.get("users.json")
        if entry is None:
            return None
        teams: Counter[str] = Counter()
        try:
            async for raw in iter_array_elements(
                open_entry(src, entry, limits),
                max_element_bytes=self.settings.export_max_json_element_bytes,
            ):
                user = json.loads(raw)
                if isinstance(user, dict) and isinstance(user.get("team_id"), str):
                    teams[user["team_id"]] += 1
        except (JsonStreamError, ValueError) as exc:
            raise ExportRejectedError("metadata_invalid", f"users.json: {exc}") from exc
        return teams.most_common(1)[0][0] if teams else None

    async def _index_day_files(
        self,
        tenant_id: uuid.UUID,
        export_id: uuid.UUID,
        src: CoalescingSource,
        limits: ArchiveLimits,
        scan: _Scan,
    ) -> None:
        """One pass over every day file in local-header order (sequential range reads, R6), element by
        element (bounded memory): element counts (the per-unit expectation), messages whose own ts is not
        on the file's hinted day (R4 anomalies), and the thread index (every threaded message with its
        entry and array index) for thread context across files. A file that does not parse is recorded;
        the unit that reads it fails loudly later. A CRC or size failure rejects the archive."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            folders = {
                r.folder: r.conversation_id
                for r in (
                    await s.execute(
                        text(
                            "SELECT folder, conversation_id FROM export_conversations WHERE export_id = :e"
                        ),
                        {"e": export_id},
                    )
                ).all()
            }
        after = -1
        threads: list[dict[str, Any]] = []
        while True:
            async with tenant_tx(self.sessions, tenant_id) as s:
                rows = (
                    await s.execute(text(DAY_FILES_PAGE), {"e": export_id, "after": after})
                ).all()
            if not rows:
                break
            after = rows[-1].local_header_offset
            days = []
            for row in rows:
                days.append(await self._index_one(src, limits, scan, row, folders, threads))
                if len(threads) >= self.settings.export_entry_batch:
                    await self._insert_threads(tenant_id, export_id, threads)
                    threads = []
            if threads:  # a day file is recorded only once its thread rows are
                await self._insert_threads(tenant_id, export_id, threads)
                threads = []
            await self._insert_days(tenant_id, export_id, days)
            await self.hooks.hit("validate:after_day_files")

    async def _index_one(
        self,
        src: CoalescingSource,
        limits: ArchiveLimits,
        scan: _Scan,
        row: Any,
        folders: Mapping[str, str],
        threads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entry = entry_from_row(row)
        conversation = folders.get(row.folder)
        elements: int | None = 0
        anomalies = 0
        error: str | None = None
        found: list[dict[str, Any]] = []
        try:
            index = -1
            async for raw in iter_array_elements(
                open_entry(src, entry, limits),
                max_element_bytes=self.settings.export_max_json_element_bytes,
            ):
                index += 1
                message = json.loads(raw)
                ts = message.get("ts") if isinstance(message, dict) else None
                try:
                    sent = ts_datetime(ts) if isinstance(ts, str) else None
                except ValueError:
                    sent = None
                if sent is None:
                    scan.elements_without_ts += 1
                    continue
                if sent.date() != row.hint_day:
                    anomalies += 1
                    if len(scan.anomaly_sample) < SAMPLE:
                        scan.anomaly_sample.append(f"{entry.name}: {ts}")
                thread_ts = message.get("thread_ts")
                if conversation is not None and isinstance(thread_ts, str):
                    found.append(
                        {"entry": entry.index, "element": index, "conv": conversation,
                         "thread": thread_ts, "ts": ts}
                    )  # fmt: skip
            elements = index + 1
        except (JsonStreamError, ValueError) as exc:
            elements, error = None, str(exc)[:500]
            scan.unparseable += 1
            if len(scan.unparseable_sample) < SAMPLE:
                scan.unparseable_sample.append(entry.name)
            found = []
        scan.messages += elements or 0
        scan.anomalies += anomalies
        scan.threaded += len(found)
        threads.extend(found)
        return {"entry": entry.index, "elements": elements, "anomalies": anomalies, "error": error}

    async def _insert_days(
        self, tenant_id: uuid.UUID, export_id: uuid.UUID, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        cols = {k: [r[k] for r in rows] for k in rows[0]}
        async with tenant_tx(self.sessions, tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO export_day_files (tenant_id, export_id, entry_idx, elements, anomalies,"
                    " parse_error) SELECT CAST(:t AS uuid), CAST(:e AS uuid), * FROM unnest("
                    " CAST(:entry AS bigint[]), CAST(:elements AS integer[]),"
                    " CAST(:anomalies AS integer[]), CAST(:error AS text[])) ON CONFLICT DO NOTHING"
                ),
                {"t": tenant_id, "e": export_id, **cols},
            )

    async def _insert_threads(
        self, tenant_id: uuid.UUID, export_id: uuid.UUID, rows: list[dict[str, Any]]
    ) -> None:
        cols = {k: [r[k] for r in rows] for k in rows[0]}
        async with tenant_tx(self.sessions, tenant_id) as s:
            await s.execute(
                text(
                    "INSERT INTO export_threads (tenant_id, export_id, entry_idx, element_idx,"
                    " conversation_id, thread_ts, ts) SELECT CAST(:t AS uuid), CAST(:e AS uuid), *"
                    " FROM unnest(CAST(:entry AS bigint[]), CAST(:element AS integer[]),"
                    " CAST(:conv AS text[]), CAST(:thread AS text[]), CAST(:ts AS text[]))"
                    " ON CONFLICT DO NOTHING"
                ),
                {"t": tenant_id, "e": export_id, **cols},
            )

    async def _scan_directory(
        self,
        tenant_id: uuid.UUID,
        export_id: uuid.UUID,
        src: CoalescingSource,
        limits: ArchiveLimits,
    ) -> _Scan:
        directory = await locate_directory(src, limits)
        # pass 1 (directory only, constant memory): is everything inside one wrapper folder?
        detector = RootDetector()
        async for e in iter_central_directory(src, limits, directory):
            detector.feed(e.name, e.is_dir)
        scan = _Scan(root=detector.root())
        batch: list[dict[str, Any]] = []
        async for e in iter_central_directory(src, limits, directory):
            rel = relative(e.name, scan.root)
            junk = is_junk(e.name)
            placement = Placement("unknown") if junk or rel is None else classify(rel, e.is_dir)
            scan.entries += 1
            scan.kinds[placement.kind] += 1
            if e.name_encoding not in (NameEncoding.ASCII, NameEncoding.UTF8):
                scan.encodings[e.name_encoding.value] += 1
                sample = scan.encoding_samples.setdefault(e.name_encoding.value, [])
                if len(sample) < SAMPLE:
                    sample.append(e.name)
            if junk:
                scan.junk += 1
                if len(scan.junk_sample) < SAMPLE:
                    scan.junk_sample.append(e.name)
            if placement.kind == "metadata" and rel is not None:
                if rel in KNOWN_METADATA:
                    scan.metadata[rel] = e
                else:
                    scan.unrecognised_metadata_count += 1
                    if len(scan.unrecognised_metadata) < SAMPLE:
                        scan.unrecognised_metadata.append(e.name)
            elif placement.kind == "unknown":
                scan.nested_metadata |= rel is not None and not junk and nested_metadata(rel)
                if len(scan.unknown) < SAMPLE:
                    scan.unknown.append(e.name)
            batch.append(
                {"idx": e.index, "name": e.name, "folded": fold_name(e.name), "kind": placement.kind,
                 "folder": placement.folder, "day": placement.hint_day, "method": e.method,
                 "crc": e.crc32, "csize": e.compressed_size, "usize": e.uncompressed_size,
                 "offset": e.local_header_offset, "raw": e.raw_name,
                 "encoding": e.name_encoding.value, "flags": e.flags}
            )  # fmt: skip
            if len(batch) >= self.settings.export_entry_batch:
                await self._insert_entries(tenant_id, export_id, batch)
                await self.hooks.hit("validate:after_entries_batch")
                batch = []
        if batch:
            await self._insert_entries(tenant_id, export_id, batch)
            await self.hooks.hit("validate:after_entries_batch")
        await self._check_overlaps(tenant_id, export_id)
        return scan

    async def _insert_entries(
        self, tenant_id: uuid.UUID, export_id: uuid.UUID, batch: list[dict[str, Any]]
    ) -> None:
        """One statement per batch. A retried batch is a no-op (same idx); a folded duplicate of any
        earlier name, in this batch or a previous one, violates the unique folded name."""
        seen: dict[str, str] = {}
        for r in batch:
            if r["folded"] in seen:
                raise _duplicate(seen[r["folded"]], r["name"])
            seen[r["folded"]] = r["name"]
        cols = {k: [r[k] for r in batch] for k in batch[0]}
        try:
            async with tenant_tx(self.sessions, tenant_id) as s:
                await s.execute(
                    text(
                        "INSERT INTO export_entries (tenant_id, export_id, idx, name, folded_name, kind,"
                        " folder, hint_day, method, crc32, compressed_size, uncompressed_size,"
                        " local_header_offset, raw_name, name_encoding, flags)"
                        " SELECT CAST(:t AS uuid), CAST(:e AS uuid), * FROM unnest(CAST(:idx AS bigint[]), CAST(:name AS text[]),"
                        " CAST(:folded AS text[]), CAST(:kind AS text[]), CAST(:folder AS text[]),"
                        " CAST(:day AS date[]), CAST(:method AS smallint[]), CAST(:crc AS bigint[]),"
                        " CAST(:csize AS bigint[]), CAST(:usize AS bigint[]), CAST(:offset AS bigint[]),"
                        " CAST(:raw AS bytea[]), CAST(:encoding AS text[]), CAST(:flags AS integer[]))"
                        " ON CONFLICT (export_id, idx) DO NOTHING"
                    ),
                    {"t": tenant_id, "e": export_id, **cols},
                )
        except IntegrityError as exc:
            if "uq_export_entries_export_id_folded_name" not in str(exc.orig):
                raise
            async with tenant_tx(self.sessions, tenant_id) as s:
                clash = (
                    await s.execute(
                        text(
                            "SELECT name, folded_name FROM export_entries WHERE export_id = :e"
                            " AND folded_name = ANY(:f) LIMIT 1"
                        ),
                        {"e": export_id, "f": cols["folded"]},
                    )
                ).one()
            raise _duplicate(clash.name, seen[clash.folded_name]) from exc

    async def _check_overlaps(self, tenant_id: uuid.UUID, export_id: uuid.UUID) -> None:
        """Entries whose local header starts inside the previous entry's header and data (the classic
        overlapping-entries bomb). Lower bound per entry: 30-byte header + compressed data; the exact
        check (names, extra fields) runs on every read (``open_entry(data_end_limit=...)``)."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            hit = (
                await s.execute(
                    text(
                        "SELECT idx, name FROM (SELECT idx, name, local_header_offset AS start,"
                        " lag(local_header_offset + 30 + compressed_size)"
                        " OVER (ORDER BY local_header_offset, idx) AS prev_end"
                        " FROM export_entries WHERE export_id = :e) t"
                        " WHERE start < prev_end LIMIT 1"
                    ),
                    {"e": export_id},
                )
            ).one_or_none()
        if hit is not None:
            raise ArchiveError(
                ArchiveErrorCode.OVERLAP,
                f"entry {hit.idx} {hit.name!r} starts inside the previous entry",
            )

    async def _load_conversations(
        self,
        tenant_id: uuid.UUID,
        export_id: uuid.UUID,
        src: CoalescingSource,
        limits: ArchiveLimits,
        scan: _Scan,
    ) -> Counter[str]:
        """Stream each conversation metadata file element by element into ``export_conversations``."""
        counts: Counter[str] = Counter()
        for filename, kind in CONVERSATION_FILES.items():
            entry = scan.metadata.get(filename)
            if entry is None:
                continue
            batch: list[dict[str, Any]] = []
            try:
                async for raw in iter_array_elements(
                    open_entry(src, entry, limits),
                    max_element_bytes=self.settings.export_max_json_element_bytes,
                ):
                    try:
                        element = json.loads(raw)
                    except ValueError as exc:
                        raise ExportRejectedError(
                            "metadata_invalid", f"{filename}: element {counts['elements']}: {exc}"
                        ) from exc
                    counts["elements"] += 1
                    rec = conversation_record(kind, element)
                    if rec is None:
                        counts["invalid_records"] += 1
                        continue
                    batch.append(
                        {"cid": rec.conversation_id, "kind": rec.kind, "folder": rec.folder,
                         "name": rec.name, "entry": filename, "team": rec.team_id}
                    )  # fmt: skip
                    if len(batch) >= self.settings.export_entry_batch:
                        counts["duplicates"] += await self._insert_conversations(
                            tenant_id, export_id, batch
                        )
                        batch = []
            except JsonStreamError as exc:
                raise ExportRejectedError("metadata_invalid", f"{filename}: {exc}") from exc
            if batch:
                counts["duplicates"] += await self._insert_conversations(
                    tenant_id, export_id, batch
                )
        return counts

    async def _insert_conversations(
        self, tenant_id: uuid.UUID, export_id: uuid.UUID, batch: list[dict[str, Any]]
    ) -> int:
        """Returns how many records were duplicates of an id already listed (first listing wins).
        A retried batch counts its own rows as duplicates; the final counts are recomputed from the
        table in ``_findings`` where it matters."""
        cols = {k: [r[k] for r in batch] for k in batch[0]}
        async with tenant_tx(self.sessions, tenant_id) as s:
            inserted = (
                await s.execute(
                    text(
                        "INSERT INTO export_conversations (tenant_id, export_id, conversation_id, kind,"
                        " folder, name, metadata_entry, team_id) SELECT CAST(:t AS uuid), CAST(:e AS uuid), *"
                        " FROM unnest(CAST(:cid AS text[]), CAST(:kind AS text[]), CAST(:folder AS text[]),"
                        " CAST(:name AS text[]), CAST(:entry AS text[]), CAST(:team AS text[]))"
                        " ON CONFLICT (export_id, conversation_id) DO NOTHING RETURNING 1"
                    ),
                    {"t": tenant_id, "e": export_id, **cols},
                )
            ).all()
        return len(batch) - len(inserted)

    async def _findings(
        self, tenant_id: uuid.UUID, export_id: uuid.UUID, scan: _Scan, records: Counter[str]
    ) -> dict[str, Any]:
        """Archive-level accounting (ADR 0014 section 4): nothing in the export goes unmentioned."""
        async with tenant_tx(self.sessions, tenant_id) as s:

            async def listing(name: str) -> dict[str, Any]:
                count_sql, sample_sql = LISTINGS[name]
                count: int = (await s.execute(text(count_sql), {"e": export_id})).scalar_one()
                sample: Sequence[str] = (
                    (await s.execute(text(sample_sql), {"e": export_id})).scalars().all()
                )
                return {"count": count, "sample": list(sample)}

            orphan_folders = await listing("folders_without_conversation")
            empty_conversations = await listing("conversations_without_messages")
            shared_folders = await listing("folders_listed_twice")
            conversations: int = (
                await s.execute(
                    text("SELECT count(*) FROM export_conversations WHERE export_id = :e"),
                    {"e": export_id},
                )
            ).scalar_one()
            teams = (
                await s.execute(
                    text(
                        "SELECT count(DISTINCT team_id) AS teams, count(*) FILTER (WHERE team_id IS NULL)"
                        " AS without FROM export_conversations WHERE export_id = :e"
                    ),
                    {"e": export_id},
                )
            ).one()
        return {
            "entries_by_kind": dict(scan.kinds),
            "unknown_entries": {"count": scan.kinds["unknown"], "sample": scan.unknown},
            "unrecognised_metadata": {
                "count": scan.unrecognised_metadata_count,
                "sample": scan.unrecognised_metadata,
            },
            "metadata_files": sorted(scan.metadata),
            "root_prefix": scan.root,
            "workspace_id": scan.workspace,
            # items are namespaced by the conversation's own team where its record names one, else by
            # workspace_id (ADR 0014 section 7; confirm on real export for Enterprise Grid)
            "conversation_teams": {"distinct": teams.teams, "without_team": teams.without},
            "messages": scan.messages,
            "threaded_messages": scan.threaded,
            "ts_outside_hint_day": {"count": scan.anomalies, "sample": scan.anomaly_sample},
            "elements_without_ts": scan.elements_without_ts,
            "day_files_unparseable": {"count": scan.unparseable, "sample": scan.unparseable_sample},
            "os_metadata_entries": {"count": scan.junk, "sample": scan.junk_sample},
            "name_encodings": {
                enc: {"count": n, "sample": scan.encoding_samples[enc]}
                for enc, n in sorted(scan.encodings.items())
            },
            "conversations": conversations,
            "invalid_metadata_records": records["invalid_records"],
            "folders_without_conversation": orphan_folders,
            "conversations_without_messages": empty_conversations,
            "folders_listed_twice": shared_folders,
        }

    async def _reject(
        self, tenant_id: uuid.UUID, export_id: uuid.UUID, code: str, detail: str
    ) -> dict[str, Any]:
        finding = {"code": code, "detail": detail}
        async with tenant_tx(self.sessions, tenant_id) as s:
            done = await s.execute(
                text(
                    "UPDATE slack_exports SET status = 'rejected', reject_reason = 'archive_invalid',"
                    " reject_detail = CAST(:d AS jsonb), validated_at = now(), updated_at = now()"
                    " WHERE id = :i AND status = 'validating'"
                ),
                {"d": json.dumps(finding), "i": export_id},
            )
            if done.rowcount == 1:  # type: ignore[attr-defined]
                await self._audit(
                    s, tenant_id, "export_rejected",
                    {"export_id": str(export_id), "reason": "archive_invalid", **finding},
                )  # fmt: skip
        await anchor_if_due(
            self.sessions, self.s3, self.settings, tenant_id=tenant_id, stream_id=tenant_id
        )
        log.warning("export rejected", export_id=str(export_id), code=code)
        return {"status": "rejected", **finding}

    # ------------------------------------------------------------------ helpers
    async def _row(self, tenant_id: uuid.UUID, export_id: uuid.UUID) -> Any:
        async with tenant_tx(self.sessions, tenant_id) as s:
            return (
                await s.execute(
                    text(
                        "SELECT x.status, x.client_id, x.staging_key, x.upload_id, x.declared_size,"
                        " x.declared_sha256, x.declared_plan, x.limits, x.created_by, x.size_bytes,"
                        " x.version_id, e.storage_key FROM slack_exports x"
                        " LEFT JOIN evidence_objects e ON e.id = x.evidence_object_id WHERE x.id = :i"
                    ),
                    {"i": export_id},
                )
            ).one()

    @staticmethod
    async def _audit(
        s: AsyncSession, tenant_id: uuid.UUID, event: str, payload: dict[str, Any]
    ) -> None:
        await append(
            s,
            tenant_id=tenant_id,
            stream_id=tenant_id,
            event_type=f"audit.{event}",
            actor=ACTOR,
            payload=payload,
        )


DAY_FILES_PAGE = (
    "SELECT idx, name, kind, method, flags, crc32, compressed_size, uncompressed_size,"
    " local_header_offset, raw_name, name_encoding, folder, hint_day FROM export_entries"
    " WHERE export_id = :e AND kind = 'day' AND local_header_offset > :after"
    " ORDER BY local_header_offset LIMIT 500"
)
# finding -> (count query, sample query)
LISTINGS = {
    "folders_without_conversation": (
        "SELECT count(DISTINCT e.folder) FROM export_entries e WHERE e.export_id = :e"
        " AND e.kind = 'day' AND NOT EXISTS (SELECT 1 FROM export_conversations c"
        " WHERE c.export_id = e.export_id AND c.folder = e.folder)",
        "SELECT DISTINCT e.folder FROM export_entries e WHERE e.export_id = :e"
        " AND e.kind = 'day' AND NOT EXISTS (SELECT 1 FROM export_conversations c"
        " WHERE c.export_id = e.export_id AND c.folder = e.folder) ORDER BY e.folder LIMIT 100",
    ),
    "conversations_without_messages": (
        "SELECT count(*) FROM export_conversations c WHERE c.export_id = :e AND NOT EXISTS"
        " (SELECT 1 FROM export_entries e WHERE e.export_id = c.export_id AND e.kind = 'day'"
        " AND e.folder = c.folder)",
        "SELECT c.conversation_id FROM export_conversations c WHERE c.export_id = :e AND NOT EXISTS"
        " (SELECT 1 FROM export_entries e WHERE e.export_id = c.export_id AND e.kind = 'day'"
        " AND e.folder = c.folder) ORDER BY c.conversation_id LIMIT 100",
    ),
    "folders_listed_twice": (
        "SELECT count(*) FROM (SELECT folder FROM export_conversations WHERE export_id = :e"
        " GROUP BY folder HAVING count(*) > 1) q",
        "SELECT folder FROM export_conversations WHERE export_id = :e GROUP BY folder"
        " HAVING count(*) > 1 ORDER BY folder LIMIT 100",
    ),
}


@dataclass
class ExportActivities:
    sessions: async_sessionmaker[AsyncSession]
    s3: S3Client
    settings: Settings

    def _ingest(self) -> ExportIngest:
        return ExportIngest(self.sessions, self.s3, self.settings)

    @activity.defn(name="lock_export")
    @_ticking
    async def lock_export(self, ref: ExportRef) -> dict[str, Any]:
        return await self._ingest().lock(uuid.UUID(ref.tenant_id), uuid.UUID(ref.export_id))

    @activity.defn(name="validate_export")
    @_ticking
    async def validate_export(self, ref: ExportRef) -> dict[str, Any]:
        return await self._ingest().validate(uuid.UUID(ref.tenant_id), uuid.UUID(ref.export_id))

    def all(self) -> list[Any]:
        return [self.lock_export, self.validate_export]
