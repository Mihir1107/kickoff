"""Persisting normalizer output, and loading the prior state it needs. Runs inside the caller's tenant
transaction (the M11 batch transaction), so items, links, custody and checkpoint commit together.

Idempotency: ``UNIQUE (tenant_id, idempotency_key)``; an existing (subject, content hash) is reused,
never duplicated. Versions: ``max(version) + 1`` per subject, assigned under a per-subject
transaction-scoped advisory lock taken in sorted order (no deadlocks between concurrent batches).
Derivations: one ``item_derivations`` row per (item, normalizer version); reprocessing with a newer
normalizer adds rows there and never touches items' evidence pointers or evidence objects.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edisc_core.canonical import canonical_hash, canonical_json
from edisc_core.idempotency import idempotency_key
from edisc_core.ids import new_id
from edisc_core.time import day_bounds
from edisc_normalizer.model import Derived, NormalizeContext, PriorState
from edisc_normalizer.slack import NO_LONGER_OBSERVED


@dataclass(frozen=True)
class PersistResult:
    inserted: int
    existing: int
    derivations_inserted: int
    item_ids: dict[str, uuid.UUID]  # idempotency_key -> item id (new or existing)


async def load_prior(
    session: AsyncSession, *, tenant_id: uuid.UUID, source: str, subjects: Iterable[str]
) -> dict[str, PriorState]:
    """Prior state for subjects (messages, '#reactions', '#observation', profiles). Reads the latest
    derivation of each item for event details, so it works across normalizer versions."""
    ids = sorted(set(subjects))
    if not ids:
        return {}
    streams = sorted({*ids, *(f"{s}#change" for s in ids)})
    rows = (
        await session.execute(
            text(
                "SELECT i.source_item_id, i.version, i.content_hash, i.change_hints, d.derived"
                " FROM items i LEFT JOIN LATERAL (SELECT derived FROM item_derivations x"
                "   WHERE x.item_id = i.id ORDER BY x.created_at DESC, x.normalizer_version DESC LIMIT 1) d ON true"
                " WHERE i.tenant_id = :t AND i.source = :s AND i.source_item_id = ANY(:ids)"
                " ORDER BY i.source_item_id, i.version"
            ),
            {"t": tenant_id, "s": source, "ids": streams},
        )
    ).all()
    by_sid: dict[str, list[Any]] = {}
    for row in rows:
        by_sid.setdefault(row.source_item_id, []).append(row)
    out: dict[str, PriorState] = {}
    for sid in ids:
        versions = by_sid.get(sid, [])
        changes = by_sid.get(f"{sid}#change", [])
        latest = versions[-1] if versions else None
        hints: dict[str, str] = dict(latest.change_hints) if latest else {}
        if latest is not None:
            for ch in changes:  # hint observations based on the latest version, applied in order
                d = ch.derived or {}
                if d.get("kind") == "hint" and d.get("base") == latest.content_hash:
                    if d.get("new") is None:
                        hints.pop(d["hint"], None)
                    else:
                        hints[d["hint"]] = d["new"]
        status, count = None, 0
        if sid.endswith(("#observation", "#availability", "#access")):
            count = len(versions)
            status = (latest.derived or {}).get("status") if latest else None
        out[sid] = PriorState(
            latest_content_hash=latest.content_hash if latest else None,
            known_content_hashes=frozenset(v.content_hash for v in versions),
            current_hints=hints,
            change_count=len(changes),
            observation_status=status,
            observation_count=count,
        )
    return out


async def previously_observed(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source: str,
    workspace_id: str,
    conversation_id: str,
    day: date,
) -> set[str]:
    """Messages of this conversation-day recorded by earlier collections and not currently marked
    no-longer-observed."""
    start, end = day_bounds(day)
    rows: Sequence[str] = (
        (
            await session.execute(
                text(
                    "SELECT DISTINCT source_item_id FROM items WHERE tenant_id = :t AND source = :s"
                    " AND item_type = 'message' AND sent_at >= :a AND sent_at < :b AND source_item_id LIKE :p"
                ),
                {
                    "t": tenant_id,
                    "s": source,
                    "a": start,
                    "b": end,
                    "p": f"{workspace_id}/{conversation_id}/%".replace("_", r"\_"),
                },
            )
        )
        .scalars()
        .all()
    )
    ids = set(rows)
    obs = await load_prior(
        session, tenant_id=tenant_id, source=source, subjects=[f"{m}#observation" for m in ids]
    )
    return {m for m in ids if obs[f"{m}#observation"].observation_status != NO_LONGER_OBSERVED}


async def persist(
    session: AsyncSession,
    *,
    ctx: NormalizeContext,
    job_id: uuid.UUID,
    connector_version: str,
    items: Sequence[Derived],
) -> PersistResult:
    """A constant number of statements per batch (not per item): advisory locks, existing keys,
    next versions, missing parents, then one multi-row INSERT each for items and derivations."""
    tenant, source = ctx.tenant_id, ctx.source
    # parents before children: messages and files, then events
    ordered = sorted(items, key=lambda d: (d.item_type.value == "event", d.source_item_id))
    keys = [d.idempotency_key(tenant, source) for d in ordered]
    sids = sorted({d.source_item_id for d in ordered})
    if not ordered:
        return PersistResult(0, 0, 0, {})
    # per-subject locks, always taken in the same (byte) order: no deadlocks between batches
    await session.execute(
        text(
            "SELECT count(pg_advisory_xact_lock(hashtextextended(k, 0)))"
            ' FROM (SELECT k FROM unnest(CAST(:ks AS text[])) AS k ORDER BY k COLLATE "C") AS locks'
        ),
        {"ks": [f"{tenant}|{source}|{sid}" for sid in sids]},
    )
    existing: dict[str, uuid.UUID] = dict(
        (
            await session.execute(
                text(
                    "SELECT idempotency_key, id FROM items WHERE tenant_id = :t AND idempotency_key = ANY(:k)"
                ),
                {"t": tenant, "k": keys},
            )
        ).all()
    )
    next_version: dict[str, int] = {
        row.source_item_id: row.v
        for row in (
            await session.execute(
                text(
                    "SELECT source_item_id, max(version) + 1 AS v FROM items WHERE tenant_id = :t AND source = :s"
                    " AND source_item_id = ANY(:ids) GROUP BY source_item_id"
                ),
                {"t": tenant, "s": source, "ids": sids},
            )
        ).all()
    }
    item_ids: dict[str, uuid.UUID] = dict(existing)
    # ids are assigned here (UUIDv7) so children in the same batch can reference their parents
    fresh: list[tuple[Derived, str]] = []
    for d, key in zip(ordered, keys, strict=True):
        if key not in item_ids:
            item_ids[key] = new_id()
            fresh.append((d, key))
    parent_keys = {
        idempotency_key(tenant, source, d.parent[0], d.parent[1])
        for d, _ in fresh
        if d.parent is not None
    }
    unknown = sorted(parent_keys - item_ids.keys())
    if unknown:
        item_ids_parents: dict[str, uuid.UUID] = dict(
            (
                await session.execute(
                    text(
                        "SELECT idempotency_key, id FROM items WHERE tenant_id = :t AND idempotency_key = ANY(:k)"
                    ),
                    {"t": tenant, "k": unknown},
                )
            ).all()
        )
    else:
        item_ids_parents = {}
    rows: dict[str, list[Any]] = {
        c: []
        for c in (
            [
                "id",
                "sid",
                "v",
                "type",
                "kind",
                "ch",
                "rh",
                "ev",
                "key",
                "path",
                "parent",
                "hints",
                "sent",
                "ik",
            ]
        )
    }
    for d, key in fresh:
        parent_id = None
        if d.parent is not None:
            parent_key = idempotency_key(tenant, source, d.parent[0], d.parent[1])
            parent_id = item_ids.get(parent_key) or item_ids_parents.get(parent_key)
            if parent_id is None:
                raise LookupError(f"{d.source_item_id}: parent {d.parent[0]} version not recorded")
        version = next_version.get(d.source_item_id, 1)
        next_version[d.source_item_id] = version + 1
        for col, value in (
            ("id", item_ids[key]),
            ("sid", d.source_item_id),
            ("v", version),
            ("type", d.item_type.value),
            ("kind", d.event_kind.value if d.event_kind else None),
            ("ch", d.content_hash),
            ("rh", d.raw_hash),
            ("ev", d.evidence.evidence_id),
            ("key", d.evidence.storage_key),
            ("path", d.json_path),
            ("parent", parent_id),
            ("hints", canonical_json(dict(d.change_hints)).decode()),
            ("sent", d.sent_at),
            ("ik", key),
        ):
            rows[col].append(value)
    if fresh:
        await session.execute(
            text(
                "INSERT INTO items (id, tenant_id, job_id, source, source_item_id, version, item_type, event_kind,"
                " content_hash, raw_hash, evidence_object_id, storage_key, json_path, parent_item_id,"
                " change_hints, sent_at, connector_version, normalizer_version, idempotency_key)"
                " SELECT r.id, :t, :j, :s, r.sid, r.v, r.type, r.kind, r.ch, r.rh, r.ev, r.key, r.path, r.parent,"
                " CAST(r.hints AS jsonb), r.sent, :cv, :nv, r.ik"
                " FROM unnest(CAST(:id AS uuid[]), CAST(:sid AS text[]), CAST(:v AS integer[]),"
                " CAST(:type AS text[]), CAST(:kind AS text[]), CAST(:ch AS text[]), CAST(:rh AS text[]),"
                " CAST(:ev AS uuid[]), CAST(:key AS text[]), CAST(:path AS text[]), CAST(:parent AS uuid[]),"
                " CAST(:hints AS text[]), CAST(:sent AS timestamptz[]), CAST(:ik AS text[]))"
                " AS r(id, sid, v, type, kind, ch, rh, ev, key, path, parent, hints, sent, ik)"
            ),
            {
                "t": tenant,
                "j": job_id,
                "s": source,
                "cv": connector_version,
                "nv": ctx.normalizer_version,
                **rows,
            },
        )
    derived_docs = [dict(d.derived) for d in ordered]
    result = await session.execute(
        text(
            "INSERT INTO item_derivations (tenant_id, item_id, normalizer_version, derived, derived_hash)"
            " SELECT :t, r.i, :nv, CAST(r.d AS jsonb), r.h"
            " FROM unnest(CAST(:i AS uuid[]), CAST(:d AS text[]), CAST(:h AS text[])) AS r(i, d, h)"
            " ON CONFLICT DO NOTHING"
        ),
        {
            "t": tenant,
            "nv": ctx.normalizer_version,
            "i": [item_ids[key] for key in keys],
            "d": [canonical_json(doc).decode() for doc in derived_docs],
            "h": [canonical_hash(doc) for doc in derived_docs],
        },
    )
    derivations: int = result.rowcount  # type: ignore[attr-defined]
    return PersistResult(len(fresh), len(ordered) - len(fresh), derivations, item_ids)
