"""M10 acceptance: every epoch's recorded output equals the oracle exactly; idempotency; reprocessing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_dummy.connector import DummyConnector, scope_for_days
from edisc_connector_dummy.dataset import Dataset
from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import Connection, WorkUnit
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_normalizer.model import EvidenceRef, NormalizeContext
from edisc_normalizer.slack import ts_datetime

from ...unit.dummy.conftest import RecordingLimiter
from .harness import (
    collect_epoch,
    ingest_messages_page,
    new_job,
    new_tenant,
    recorded,
    recorded_files,
)
from .oracle import expected, project

Sessions = async_sessionmaker[AsyncSession]


def spec(dialect: str) -> DatasetSpec:
    return DatasetSpec.model_validate(
        {
            "seed": 11,
            "dialect": dialect,
            "conversations": 3,
            "days": 2,
            "messages_per_unit": 16,
            "page_size": 6,
            "users": 8,
        }
    )


def diff(want: dict[str, Any], got: dict[str, Any]) -> list[str]:
    problems = [f"missing {k}" for k in sorted(set(want) - set(got))]
    problems += [f"unexpected {k}" for k in sorted(set(got) - set(want))]
    problems += [
        f"{k}: want {want[k]!r} got {got[k]!r}"
        for k in sorted(set(want) & set(got))
        if want[k] != got[k]
    ]
    return problems


@pytest.mark.parametrize("dialect", ["slack", "slack_history"])
async def test_every_epoch_matches_the_oracle_exactly(
    app_sessions: Sessions, s3: S3Client, settings: Settings, dialect: str
) -> None:
    sp = spec(dialect)
    ds = Dataset(sp)
    tenant = await new_tenant(app_sessions)
    for epoch in (0, 1, 2):
        await collect_epoch(app_sessions, s3, settings, tenant, sp, epoch)
        want, got = expected(ds, epoch), project(await recorded(app_sessions, tenant))
        problems = diff(want, got)
        assert not problems, f"epoch {epoch}: {len(problems)} differences, first: {problems[:5]}"

    # the dataset really exercised what the oracle claims (guards against a vacuous comparison)
    kinds = {
        k.rsplit("#", 1)[-1] if "#" in k else ("file" if "/file/" in k else "message") for k in want
    }
    assert {"message", "file", "change", "reactions", "profile", "profile-embed"} <= kinds
    versions = [v for k, v in want.items() if "#" not in k and "/file/" not in k]
    assert any(len(v) > 1 for v in versions)  # edits created versions
    if dialect == "slack_history":
        assert any(
            v == ["no_longer_observed"] for k, v in want.items() if k.endswith("#observation")
        )
        assert not any(
            state[1] for v in versions for state in v
        )  # absence NEVER created a deleted version
    else:
        assert any(state[1] for v in versions for state in v)  # tombstones did
    renamed = want[f"{sp.workspace_id}/user/U00005DUMMY#profile"]
    assert len(renamed) == 2  # renamed between epochs -> identity snapshot versions
    assert (
        len(want[f"{sp.workspace_id}/user/U00000DUMMY#profile-embed"]) >= 2
    )  # renamed mid-dataset


async def test_same_page_twice_creates_zero_new_rows_and_overlaps_do_not_duplicate(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec("slack")
    ds = Dataset(sp)
    tenant = await new_tenant(app_sessions)
    await collect_epoch(app_sessions, s3, settings, tenant, sp, 0)
    assert tenant.stats["existing"] > 0  # overlapping pages repeated items: reused, not duplicated

    async with tenant_tx(app_sessions, tenant.tenant_id) as s:
        counts = (
            await s.execute(
                text(
                    "SELECT count(*) FILTER (WHERE item_type = 'message') AS msgs, count(*) AS all_items,"
                    " count(DISTINCT idempotency_key) AS keys FROM items WHERE tenant_id = :t"
                ),
                {"t": tenant.tenant_id},
            )
        ).one()
    assert counts.msgs == ds.total_messages(0)  # exactly one version per message at epoch 0
    assert counts.all_items == counts.keys

    # normalize one real page again, twice (same bytes, same evidence): nothing new at all
    connector = DummyConnector(RecordingLimiter())
    conn = Connection(
        tenant.tenant_id,
        tenant.connection_id,
        "dummy",
        sp.workspace_id,
        {"spec": sp.model_dump(mode="json"), "epoch": 0},
    )
    unit = WorkUnit(ds.conversations()[0].id, ds.day(0))
    scope = scope_for_days("*", datetime.combine(ds.day(0), time(0), tzinfo=UTC), 1)
    batch = [b async for b in connector.fetch(conn, unit, None, scope=scope)][2]
    async with tenant_tx(app_sessions, tenant.tenant_id) as s:
        ref = (
            await s.execute(
                text(
                    "SELECT id, storage_key FROM evidence_objects WHERE tenant_id = :t AND kind = 'page' LIMIT 1"
                ),
                {"t": tenant.tenant_id},
            )
        ).one()
    ctx = NormalizeContext(
        tenant.tenant_id,
        "dummy",
        sp.workspace_id,
        unit.conversation_id,
        unit.day,
        scope.date_from,
        scope.date_to,
    )
    job = await new_job(app_sessions, tenant)
    before = dict(tenant.stats)
    files = await recorded_files(app_sessions, tenant)
    writer = EvidenceWriter(app_sessions, s3, settings)
    for _ in range(2):
        await ingest_messages_page(
            app_sessions,
            writer,
            conn,
            connector,
            tenant,
            job,
            ctx,
            batch.body,
            page_ref=EvidenceRef(ref.id, ref.storage_key),
            files=files,
        )
    assert tenant.stats["inserted"] == before["inserted"]  # zero new rows
    assert tenant.stats["derivations"] == before["derivations"]


async def test_reprocessing_with_a_newer_normalizer_adds_derivations_only(
    app_sessions: Sessions, s3: S3Client, settings: Settings
) -> None:
    sp = spec("slack")
    tenant = await new_tenant(app_sessions)
    await collect_epoch(app_sessions, s3, settings, tenant, sp, 0)

    async def snapshot() -> tuple[int, int, int, list[Any]]:
        async with tenant_tx(app_sessions, tenant.tenant_id) as s:
            items = (
                await s.execute(
                    text("SELECT count(*) FROM items WHERE tenant_id = :t"), {"t": tenant.tenant_id}
                )
            ).scalar_one()
            derivs = (
                await s.execute(
                    text("SELECT count(*) FROM item_derivations WHERE tenant_id = :t"),
                    {"t": tenant.tenant_id},
                )
            ).scalar_one()
            evid = (
                await s.execute(
                    text(
                        "SELECT id, storage_key, state, sha256, version_id, retain_until FROM evidence_objects"
                        " WHERE tenant_id = :t ORDER BY id"
                    ),
                    {"t": tenant.tenant_id},
                )
            ).all()
        versions = 0
        for row in evid:
            listed = await s3.list_object_versions(
                Bucket=settings.s3_evidence_bucket, Prefix=row.storage_key
            )
            versions += len(listed.get("Versions", []))
        return items, derivs, versions, [tuple(r) for r in evid]

    items0, derivs0, s3_versions0, evidence0 = await snapshot()
    writer = EvidenceWriter(app_sessions, s3, settings)
    connector = DummyConnector(RecordingLimiter())
    conn = Connection(
        tenant.tenant_id,
        tenant.connection_id,
        "dummy",
        sp.workspace_id,
        {"spec": sp.model_dump(mode="json"), "epoch": 0},
    )
    job = await new_job(app_sessions, tenant)
    files = await recorded_files(app_sessions, tenant)  # never re-fetched, never re-written
    async with tenant_tx(app_sessions, tenant.tenant_id) as s:
        pages = (
            await s.execute(
                text(
                    "SELECT DISTINCT e.id, e.storage_key, split_part(i.source_item_id, '/', 2) AS conv"
                    " FROM evidence_objects e JOIN items i ON i.evidence_object_id = e.id"
                    " WHERE e.tenant_id = :t AND e.kind = 'page' AND i.item_type = 'message'"
                ),
                {"t": tenant.tenant_id},
            )
        ).all()
    for page in pages:
        body = b"".join(
            [c async for c in writer.open(tenant_id=tenant.tenant_id, evidence_id=page.id)]
        )
        day = ts_datetime(json.loads(body)["messages"][0]["ts"]).date()
        ctx = NormalizeContext(
            tenant.tenant_id,
            "dummy",
            sp.workspace_id,
            page.conv,
            day,
            None,
            None,
            normalizer_version="0.2.0-test",
        )
        await ingest_messages_page(
            app_sessions,
            writer,
            conn,
            connector,
            tenant,
            job,
            ctx,
            body,
            page_ref=EvidenceRef(page.id, page.storage_key),
            files=files,
        )
    items1, derivs1, s3_versions1, evidence1 = await snapshot()
    assert items1 == items0  # same fingerprints: no new versions
    assert derivs1 > derivs0  # new derived records under the new normalizer version
    assert evidence1 == evidence0  # no evidence row changed (incl. retention)
    assert s3_versions1 == s3_versions0  # and no object was written
    newer = await recorded(app_sessions, tenant, "0.2.0-test")
    older = await recorded(app_sessions, tenant)
    assert set(newer) <= set(older)
