"""Collect a dummy epoch into the real DB + WORM through the normalizer (a minimal M11 stand-in:
no Temporal, no custody linkage yet)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import BatchKind, Connection
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_core.time import utc_now
from edisc_db.session import create_tenant, tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_normalizer.model import NORMALIZER_VERSION, EvidenceRef, FileEvidence, NormalizeContext
from edisc_normalizer.slack import (
    directory_page_subjects,
    file_refs,
    finalize_unit,
    message_page_subjects,
    messages_fragment_hash,
    normalize_directory_page,
    normalize_messages_page,
)
from edisc_normalizer.store import load_prior, persist, previously_observed

from ...unit.dummy.conftest import RecordingLimiter

Sessions = async_sessionmaker[AsyncSession]


@dataclass
class Tenant:
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    connection_id: uuid.UUID
    retention: datetime
    stats: dict[str, int] = field(
        default_factory=lambda: {"inserted": 0, "existing": 0, "derivations": 0, "pages": 0}
    )


async def new_tenant(sessions: Sessions) -> Tenant:
    t = Tenant(new_id(), new_id(), new_id(), utc_now() + timedelta(days=30))
    await create_tenant(
        sessions,
        tenant_id=t.tenant_id,
        name="N",
        subdomain=f"n-{secrets.token_hex(6)}",
        kms_key_ref="local:k",
    )
    async with tenant_tx(sessions, t.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO matters (id, tenant_id, name, retention_until) VALUES (:m, :t, 'M', :r)"
            ),
            {"m": t.matter_id, "t": t.tenant_id, "r": t.retention},
        )
        await s.execute(
            text(
                "INSERT INTO connections (id, tenant_id, source, external_org_id, status)"
                " VALUES (:c, :t, 'dummy', 'org', 'active')"
            ),
            {"c": t.connection_id, "t": t.tenant_id},
        )
    return t


async def new_job(sessions: Sessions, t: Tenant) -> uuid.UUID:
    job_id = new_id()
    async with tenant_tx(sessions, t.tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO collection_jobs (id, tenant_id, matter_id, connection_id, status, connector_version, requested_by)"
                " VALUES (:j, :t, :m, :c, 'running', '0.1.0', 'harness')"
            ),
            {"j": job_id, "t": t.tenant_id, "m": t.matter_id, "c": t.connection_id},
        )
    return job_id


async def one_shot(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def ingest_messages_page(
    sessions: Sessions,
    writer: EvidenceWriter,
    conn: Connection,
    connector: DummyConnector,
    t: Tenant,
    job: uuid.UUID,
    ctx: NormalizeContext,
    body: bytes,
    *,
    page_ref: EvidenceRef | None = None,
    files: dict[str, FileEvidence] | None = None,
) -> tuple[frozenset[str], EvidenceRef]:
    if page_ref is None:
        page = await writer.write_page(
            tenant_id=t.tenant_id,
            job_id=job,
            matter_retention_until=t.retention,
            stream=one_shot(body),
        )
        page_ref = EvidenceRef(page.evidence_id, page.storage_key)
    if files is None:
        files = await fetch_files(writer, conn, connector, t, job, body)
    async with tenant_tx(sessions, t.tenant_id) as s:
        prior = await load_prior(
            s, tenant_id=t.tenant_id, source="dummy", subjects=message_page_subjects(body, ctx=ctx)
        )
        result = normalize_messages_page(body, ctx=ctx, page_ref=page_ref, prior=prior, files=files)
        stats = await persist(s, ctx=ctx, job_id=job, connector_version="0.1.0", items=result.items)
    t.stats["inserted"] += stats.inserted
    t.stats["existing"] += stats.existing
    t.stats["derivations"] += stats.derivations_inserted
    t.stats["pages"] += 1
    return result.observed_messages, page_ref


async def fetch_files(
    writer: EvidenceWriter,
    conn: Connection,
    connector: DummyConnector,
    t: Tenant,
    job: uuid.UUID,
    body: bytes,
) -> dict[str, FileEvidence]:
    files: dict[str, FileEvidence] = {}
    for fm in file_refs(body):
        fw = await writer.write_file(
            tenant_id=t.tenant_id,
            job_id=job,
            matter_retention_until=t.retention,
            stream=connector.open_file(conn, fm.file_id),
        )
        files[fm.file_id] = FileEvidence(
            fm.file_id, fw.sha256, fw.size, EvidenceRef(fw.evidence_id, fw.storage_key)
        )
    return files


async def recorded_files(sessions: Sessions, t: Tenant) -> dict[str, FileEvidence]:
    """File evidence as already recorded (for reprocessing: never re-fetch, never re-write)."""
    async with tenant_tx(sessions, t.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT split_part(i.source_item_id, '/file/', 2) AS file_id, e.id, e.storage_key, e.sha256, e.size_bytes"
                    " FROM items i JOIN evidence_objects e ON e.id = i.evidence_object_id"
                    " WHERE i.tenant_id = :t AND i.item_type = 'file'"
                ),
                {"t": t.tenant_id},
            )
        ).all()
    return {
        r.file_id: FileEvidence(r.file_id, r.sha256, r.size_bytes, EvidenceRef(r.id, r.storage_key))
        for r in rows
    }


async def collect_epoch(
    sessions: Sessions, s3: S3Client, settings: Settings, t: Tenant, spec: DatasetSpec, epoch: int
) -> uuid.UUID:
    ds = Dataset(spec)
    connector = DummyConnector(RecordingLimiter())
    conn = Connection(
        t.tenant_id,
        t.connection_id,
        "dummy",
        spec.workspace_id,
        {"spec": spec.model_dump(mode="json"), "epoch": epoch},
    )
    writer = EvidenceWriter(sessions, s3, settings)
    job = await new_job(sessions, t)
    first = datetime.combine(ds.day(0), datetime.min.time(), tzinfo=UTC)
    scope = scope_for_days("*", first, ds.n_days(epoch))
    async for unit in connector.enumerate(conn, scope):
        ctx = NormalizeContext(
            t.tenant_id,
            "dummy",
            spec.workspace_id,
            unit.conversation_id,
            unit.day,
            scope.date_from,
            scope.date_to,
        )
        observed: set[str] = set()
        last: tuple[bytes, EvidenceRef] | None = None
        async for batch in connector.fetch(conn, unit, None, scope=scope):
            seen, ref = await ingest_messages_page(
                sessions, writer, conn, connector, t, job, ctx, batch.body
            )
            if batch.kind is BatchKind.HISTORY:
                observed |= seen
                last = (batch.body, ref)
        assert last is not None
        async with tenant_tx(sessions, t.tenant_id) as s:
            before = await previously_observed(
                s,
                tenant_id=t.tenant_id,
                source="dummy",
                workspace_id=spec.workspace_id,
                conversation_id=unit.conversation_id,
                day=unit.day,
            )
            missing = before - observed
            prior = await load_prior(
                s,
                tenant_id=t.tenant_id,
                source="dummy",
                subjects=[*missing, *(f"{m}#observation" for m in missing)],
            )
            absent = finalize_unit(
                ctx=ctx,
                previously_observed=before,
                observed=observed,
                prior=prior,
                last_page_fragment_hash=messages_fragment_hash(last[0]),
                last_page_ref=last[1],
            )
            await persist(s, ctx=ctx, job_id=job, connector_version="0.1.0", items=absent)
    dctx = NormalizeContext(t.tenant_id, "dummy", spec.workspace_id, None, None, None, None)
    async for batch in connector.fetch_directory(conn, None):
        page = await writer.write_page(
            tenant_id=t.tenant_id,
            job_id=job,
            matter_retention_until=t.retention,
            stream=one_shot(batch.body),
        )
        async with tenant_tx(sessions, t.tenant_id) as s:
            prior = await load_prior(
                s,
                tenant_id=t.tenant_id,
                source="dummy",
                subjects=directory_page_subjects(batch.body, ctx=dctx),
            )
            result = normalize_directory_page(
                batch.body,
                ctx=dctx,
                page_ref=EvidenceRef(page.evidence_id, page.storage_key),
                prior=prior,
            )
            await persist(s, ctx=dctx, job_id=job, connector_version="0.1.0", items=result.items)
    return job


async def recorded(
    sessions: Sessions, t: Tenant, normalizer_version: str = NORMALIZER_VERSION
) -> dict[str, list[dict[str, Any]]]:
    """source_item_id -> derived records of its versions, in version order."""
    async with tenant_tx(sessions, t.tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT i.source_item_id, i.version, d.derived FROM items i JOIN item_derivations d ON d.item_id = i.id"
                    " WHERE i.tenant_id = :t AND d.normalizer_version = :nv ORDER BY i.source_item_id, i.version"
                ),
                {"t": t.tenant_id, "nv": normalizer_version},
            )
        ).all()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row.source_item_id, []).append(dict(row.derived))
    return out
