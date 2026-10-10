"""The collection report's loader (ADR 0018 §7, §8, §12; M16 step 1): reads the verified job chain and
the database, feeds the pure model (`edisc_renderers.report.model`), and streams the files.

Passes, each in keyset pages with short transactions and no transaction across S3 I/O:
1. **Verify** the job chain with the seal required (`verify_chain`), and list the seal from S3.
2. **Chain pass:** every job-stream event in seq order into `ChainFold` (job facts, pauses, final
   status, actors, the digest of every unit fact).
3. **Database pass:** every settled `work_units` row as the unit fact it should match, into a second
   `DigestFold`. Only the differing buckets are re-read on both sides and compared unit by unit
   (`compare_units`): those are the divergences, and the chain fact is what gets stated.
4. **Files:** `units.jsonl` (file order: conversation, day, unit key), `observations.jsonl` (unit
   key, item id), `renders.jsonl` (render id, from the snapshot), `conversations.jsonl` (only when
   there are more conversations than the cap: a second pass over the units in file order), each
   line hashed as it streams; then `report.json` and `report.html`.

Memory is O(page + cap + divergent units). CPU-bound folding runs in worker threads (ADR 0015 §24).
"""

from __future__ import annotations

import asyncio
import json
import unicodedata
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_hash, canonical_json
from edisc_core.settings import Settings
from edisc_custody.chain import ANCHOR_FORMAT, anchor_key
from edisc_custody.log import verify_chain
from edisc_db.session import tenant_tx
from edisc_evidence.worm import get_bytes, list_versions
from edisc_renderers.report import model as m
from edisc_renderers.report.html import report_html
from edisc_renderers.report.version import REPORT_RENDERER_VERSION

PAGE = 2_000

_UNIT_COLUMNS = (
    "SELECT w.unit_key, w.kind, w.conversation_id, w.day, w.status, w.recon_status,"
    " w.expected_count, w.collected_count, w.file_gaps, w.day_anomalies, w.last_error,"
    " w.access_lost_reason FROM work_units w WHERE w.job_id = :j"
)
_UNITS = {
    ("key", False): _UNIT_COLUMNS + " ORDER BY w.unit_key LIMIT :n",
    ("key", True): _UNIT_COLUMNS + " AND w.unit_key > :k ORDER BY w.unit_key LIMIT :n",
    ("file", False): _UNIT_COLUMNS + " ORDER BY w.conversation_id, w.day, w.unit_key LIMIT :n",
    ("file", True): _UNIT_COLUMNS + " AND (w.conversation_id, w.day, w.unit_key) > (:c, :d, :k)"
    " ORDER BY w.conversation_id, w.day, w.unit_key LIMIT :n",
}
_OBSERVATION_COLUMNS = (
    "SELECT ji.unit_key, ji.item_id, i.event_kind, i.source_item_id FROM job_items ji"
    " JOIN items i ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id"
    " WHERE ji.job_id = :j AND i.event_kind = ANY(:k)"
)
_OBSERVATIONS = _OBSERVATION_COLUMNS + " ORDER BY ji.unit_key, ji.item_id LIMIT :n"
_OBSERVATIONS_AFTER = (
    _OBSERVATION_COLUMNS
    + " AND (ji.unit_key, ji.item_id) > (:u0, :u1) ORDER BY ji.unit_key, ji.item_id LIMIT :n"
)

Sink = Callable[[str, AsyncIterator[bytes]], Awaitable[None]]
"""Receives each file (name, its bytes as a stream); the stream must be consumed in full."""
FileSink = Callable[
    [str, Callable[[], AsyncIterator[bytes]], Callable[[], "int | None"]], Awaitable[None]
]
"""Receives each file (name, a factory of its bytes, its row count after a full stream)."""


class ReportRefusedError(Exception):
    """The job cannot have a report (yet): it is not sealed (`report_refused`)."""


@dataclass
class BuiltReport:
    report_json: bytes
    document: dict[str, Any]
    files: list[m.JsonlDigest]
    divergences: list[m.Divergence]
    clean: bool


@dataclass
class _UnitsPass:
    status_counts: Counter[str] = field(default_factory=Counter)
    recon_counts: Counter[str] = field(default_factory=Counter)
    totals: Counter[str] = field(default_factory=Counter)
    exceptions: m.Capped = field(default_factory=m.Capped)
    conversations: m.ConversationFold = field(default_factory=m.ConversationFold)
    conversations_capped: m.Capped = field(default_factory=m.Capped)


def _event_row(r: Any) -> m.ChainEvent:
    return m.ChainEvent(r.seq, r.event_type, r.actor, r.created_at, r.payload)


def unit_rows_page(
    rows: Sequence[Mapping[str, Any]], *, zone: str, archive_backed: bool,
    stated: Mapping[str, m.UnitFact], divergent: set[str],
) -> list[dict[str, Any]]:  # fmt: skip
    """The `units.jsonl` rows of one page. Pure CPU work: only ever called in a worker thread
    (`OFF_LOOP`, ADR 0015 §24)."""
    out = []
    for r in rows:
        key = r["unit_key"]
        fact: m.UnitFact | None = stated.get(key)
        if fact is None and key not in divergent:
            # bucket agreed: the database fact IS the chain fact
            fact = m.unit_fact_from_row(
                r, no_longer_observed=r["nlo"], archive_backed=archive_backed
            )
        out.append(
            m.unit_row(r, stated=fact, zone=zone, scopes=r["scopes"], divergent=key in divergent)
        )
    return out


class ReportLoader:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        s3: S3Client,
        settings: Settings,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> None:
        self._sessions, self._s3, self._settings = sessions, s3, settings
        self._tenant, self._job_id = tenant_id, job_id

    # ------------------------------------------------------------------ job, verification
    async def job(self) -> Mapping[str, Any]:
        async with tenant_tx(self._sessions, self._tenant) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT j.id, j.tenant_id, m.client_id, j.matter_id, j.workspace_id,"
                        " j.connection_id, j.status, j.rerun_of, j.requested_by, j.created_at,"
                        " j.started_at, j.finished_at, j.sealed_at, j.seal_storage_key,"
                        " c.source AS connection_source, c.plan_tier, c.granted_scopes,"
                        " c.blind_spots, c.config AS connection_config"
                        " FROM collection_jobs j JOIN matters m ON m.id = j.matter_id"
                        " JOIN connections c ON c.id = j.connection_id WHERE j.id = :j"
                    ),
                    {"j": self._job_id},
                )
            ).one_or_none()
        if row is None:
            raise ReportRefusedError(f"job {self._job_id} does not exist")
        if row.sealed_at is None or row.seal_storage_key is None:
            raise ReportRefusedError(f"job {self._job_id} is {row.status} and not sealed")
        return dict(row._mapping)

    async def verification(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """`verify_chain` with the seal required, plus the seal as LISTED from S3: exactly one
        object version, anchoring the verified head. A failure is reported, never raised."""
        report = await verify_chain(
            self._sessions,
            self._s3,
            self._settings,
            tenant_id=self._tenant,
            stream_id=self._job_id,
            require_seal=True,
        )
        out = report.as_dict()
        key = str(job["seal_storage_key"])
        versions = [
            v
            async for v in list_versions(
                self._s3, bucket=self._settings.s3_evidence_bucket, prefix=key
            )
            if v.key == key
        ]
        out["seal_key"], out["seal_version_id"] = key, None
        problems = list(out["errors"])
        if key != anchor_key(str(self._tenant), str(self._job_id), report.events):
            problems.append(f"seal {key} is not the anchor of head {report.events}")
        if len(versions) != 1 or versions[0].is_delete_marker:
            problems.append(f"seal {key} has {len(versions)} object versions, expected 1")
        else:
            out["seal_version_id"] = versions[0].version_id
            doc = json.loads(
                await get_bytes(
                    self._s3,
                    bucket=self._settings.s3_evidence_bucket,
                    key=key,
                    version_id=versions[0].version_id,
                )
            )
            expected = {
                "format": ANCHOR_FORMAT,
                "tenant_id": str(self._tenant),
                "stream_id": str(self._job_id),
                "seq": report.events,
                "event_hash": report.head_hash,
            }
            if doc != expected:
                problems.append(f"seal {key} does not anchor the verified head")
        out["errors"], out["ok"] = problems, not problems
        return out

    # ------------------------------------------------------------------ chain pass
    async def _events(self, types: Sequence[str] | None = None) -> AsyncIterator[list[Any]]:
        after = 0
        while True:
            async with tenant_tx(self._sessions, self._tenant) as s:
                rows = (
                    await s.execute(
                        text(
                            "SELECT seq, event_type, actor, created_at, payload FROM custody_events"
                            " WHERE stream_id = :s AND seq > :a"
                            " AND (CAST(:all AS boolean) OR event_type = ANY(:t))"
                            " ORDER BY seq LIMIT :n"
                        ),
                        {
                            "s": self._job_id,
                            "a": after,
                            "n": PAGE,
                            "all": not types,
                            "t": list(types or ()),
                        },
                    )
                ).all()
            if not rows:
                return
            after = rows[-1].seq
            yield list(rows)

    async def chain_pass(self) -> m.ChainFold:
        fold = m.ChainFold()

        def add(rows: Sequence[Any]) -> None:
            for r in rows:
                fold.add(_event_row(r))

        async for rows in self._events():
            await asyncio.to_thread(add, rows)
        return fold

    # ------------------------------------------------------------------ database pass
    async def _unit_pages(self, order: str) -> AsyncIterator[list[Any]]:
        """Every work unit of the job with its no-longer-observed link count, in pages; ``order``
        "key" (unit key) or "file" (conversation, day, unit key: `units.jsonl`)."""
        cursor: dict[str, Any] | None = None
        while True:
            params: dict[str, Any] = {"j": self._job_id, "n": PAGE, **(cursor or {})}
            query = _UNITS[(order, cursor is not None)]
            async with tenant_tx(self._sessions, self._tenant) as s:
                rows = (await s.execute(text(query), params)).all()
                if not rows:
                    return
                keys = [r.unit_key for r in rows]
                nlo: dict[str, int] = {
                    r[0]: int(r[1])
                    for r in (
                        await s.execute(
                            text(
                                "SELECT ji.unit_key, count(*) FROM job_items ji JOIN items i"
                                " ON i.tenant_id = ji.tenant_id AND i.id = ji.item_id"
                                " WHERE ji.job_id = :j AND ji.unit_key = ANY(:u)"
                                " AND i.event_kind = 'no_longer_observed' GROUP BY ji.unit_key"
                            ),
                            {"j": self._job_id, "u": keys},
                        )
                    ).all()
                }
                scopes = await self._scopes(s, keys)
            last = rows[-1]
            cursor = {"k": last.unit_key, "c": last.conversation_id, "d": last.day}
            yield [
                {
                    **dict(r._mapping),
                    "nlo": nlo.get(r.unit_key, 0),
                    "scopes": scopes.get(r.unit_key, []),
                }
                for r in rows
            ]

    async def _scopes(self, s: AsyncSession, keys: Sequence[str]) -> dict[str, list[int]]:
        """Indexes into `job_started.scopes` (scope rows in their creation order) per unit."""
        rows = (
            await s.execute(
                text(
                    "SELECT ws.unit_key, cs.id FROM work_unit_scopes ws JOIN collection_scopes cs"
                    " ON cs.id = ws.scope_id WHERE ws.job_id = :j AND ws.unit_key = ANY(:u)"
                ),
                {"j": self._job_id, "u": list(keys)},
            )
        ).all()
        if not hasattr(self, "_scope_index"):
            ids: Any = (
                await s.execute(
                    text("SELECT id FROM collection_scopes WHERE job_id = :j ORDER BY id"),
                    {"j": self._job_id},
                )
            ).scalars()
            self._scope_index = {sid: i for i, sid in enumerate(ids)}
        out: dict[str, list[int]] = {}
        for r in rows:
            out.setdefault(r.unit_key, []).append(self._scope_index[r.id])
        return {k: sorted(v) for k, v in out.items()}

    async def database_pass(self, archive_backed: bool) -> m.DigestFold:
        fold = m.DigestFold()

        def add(rows: Sequence[Mapping[str, Any]]) -> None:
            for r in rows:
                fact = m.unit_fact_from_row(
                    r, no_longer_observed=r["nlo"], archive_backed=archive_backed
                )
                if fact is not None:
                    fold.add(fact)

        async for rows in self._unit_pages("key"):
            await asyncio.to_thread(add, rows)
        return fold

    async def resolve(
        self, buckets: Sequence[int], archive_backed: bool
    ) -> tuple[list[m.Divergence], dict[str, m.UnitFact]]:
        """Re-read the units of the differing buckets on both sides and compare them."""
        if not buckets:
            return [], {}
        wanted = set(buckets)
        chain: list[m.UnitFact] = []
        async for rows in self._events(m.UNIT_EVENTS):
            for r in rows:
                fact = m.unit_fact_from_event(r.event_type, r.payload)
                if m.bucket_of(fact.unit_key) in wanted:
                    chain.append(fact)
        db: list[m.UnitFact] = []
        async for rows in self._unit_pages("key"):
            for r in rows:
                if m.bucket_of(r["unit_key"]) not in wanted:
                    continue
                found = m.unit_fact_from_row(
                    r, no_longer_observed=r["nlo"], archive_backed=archive_backed
                )
                if found is not None:
                    db.append(found)
        return await asyncio.to_thread(m.compare_units, chain, db)

    # ------------------------------------------------------------------ the files
    async def units_file(
        self, *, zone: str, archive_backed: bool, stated: Mapping[str, m.UnitFact],
        divergent: set[str], acc: _UnitsPass, digest: m.JsonlDigest,
    ) -> AsyncIterator[bytes]:  # fmt: skip
        def page(rows: Sequence[Mapping[str, Any]]) -> bytes:
            out = []
            for row in unit_rows_page(rows, zone=zone, archive_backed=archive_backed,
                                       stated=stated, divergent=divergent):  # fmt: skip
                acc.status_counts[row["status"]] += 1
                if row["kind"] != "directory":
                    acc.recon_counts[row["recon_status"]] += 1
                for k in ("expected", "collected", "file_gaps"):
                    acc.totals[k] += int(row[k] or 0)
                severity = m.unit_severity(row)
                if severity in m.EXCEPTION_RECON:
                    acc.exceptions.add(severity, m.unit_order(row), row)
                conv = acc.conversations.add(row)
                if conv is not None:
                    acc.conversations_capped.add(
                        conv["worst_status"], (conv["conversation_id"],), conv
                    )
                out.append(digest.add(m.jsonl_line(row)))
            return b"".join(out)

        async for rows in self._unit_pages("file"):
            yield await asyncio.to_thread(page, rows)
        last = acc.conversations.finish()
        if last is not None:
            acc.conversations_capped.add(last["worst_status"], (last["conversation_id"],), last)

    async def conversations_file(
        self, *, zone: str, archive_backed: bool, stated: Mapping[str, m.UnitFact],
        divergent: set[str], capped: m.Capped, digest: m.JsonlDigest,
    ) -> AsyncIterator[bytes]:  # fmt: skip
        """`conversations.jsonl` (§1): one row per conversation in conversation-id order, folded
        from a second pass over the units in file order (memory O(page + cap))."""
        fold = m.ConversationFold()

        def take(conv: dict[str, Any] | None, out: list[bytes]) -> None:
            if conv is not None:
                capped.add(conv["worst_status"], (conv["conversation_id"],), conv)
                out.append(digest.add(m.jsonl_line(conv)))

        def page(rows: Sequence[Mapping[str, Any]]) -> bytes:
            out: list[bytes] = []
            for row in unit_rows_page(rows, zone=zone, archive_backed=archive_backed,
                                       stated=stated, divergent=divergent):  # fmt: skip
                take(fold.add(row), out)
            return b"".join(out)

        async for rows in self._unit_pages("file"):
            yield await asyncio.to_thread(page, rows)
        tail: list[bytes] = []
        take(fold.finish(), tail)
        if tail:
            yield b"".join(tail)

    async def observations_file(
        self, counts: Counter[str], by_reason: Counter[tuple[str, str]], capped: m.Capped,
        digest: m.JsonlDigest,
    ) -> AsyncIterator[bytes]:  # fmt: skip
        cursor: tuple[str, uuid.UUID] | None = None
        while True:
            params: dict[str, Any] = {"j": self._job_id, "n": PAGE, "k": list(m.OBSERVATION_KINDS)}
            if cursor is not None:
                params.update({"u0": cursor[0], "u1": cursor[1]})
            query = _OBSERVATIONS_AFTER if cursor is not None else _OBSERVATIONS
            async with tenant_tx(self._sessions, self._tenant) as s:
                rows = (await s.execute(text(query), params)).all()
                if not rows:
                    return
                derived = {
                    d.item_id: d.derived
                    for d in (
                        await s.execute(
                            text(
                                "SELECT DISTINCT ON (item_id) item_id, derived FROM item_derivations"
                                " WHERE item_id = ANY(:i)"
                                " ORDER BY item_id, string_to_array(normalizer_version, '.')::int[] DESC"
                            ),
                            {"i": [r.item_id for r in rows]},
                        )
                    ).all()
                }
            cursor = (rows[-1].unit_key, rows[-1].item_id)

            def page(rows: Sequence[Any] = rows, derived: Mapping[Any, Any] = derived) -> bytes:
                out = []
                for r in rows:
                    row = m.observation_row(
                        unit_key=r.unit_key,
                        item_id=str(r.item_id),
                        kind=r.event_kind,
                        source_item_id=r.source_item_id,
                        derived=derived.get(r.item_id, {}),
                    )
                    counts[r.event_kind] += 1
                    by_reason[(r.event_kind, str(row.get("reason")))] += 1
                    if r.event_kind in ("file_unavailable", "access_lost", "no_longer_observed"):
                        capped.add(r.event_kind, (r.unit_key, str(r.item_id)), row)
                    out.append(digest.add(m.jsonl_line(row)))
                return b"".join(out)

            yield await asyncio.to_thread(page)

    async def renders_file(
        self, renders: Sequence[Mapping[str, Any]], digest: m.JsonlDigest
    ) -> AsyncIterator[bytes]:
        for r in sorted(renders, key=lambda r: str(r["render_id"])):
            yield digest.add(m.jsonl_line(m.render_row(r)))

    # ------------------------------------------------------------------ snapshot
    async def snapshot(self) -> dict[str, Any]:
        """Every mutable input, captured once (§9): renders sealed now, retention gaps touching the
        job's evidence, the tenant audit head. (Step 4 stores it write-once on the report row.)"""
        async with tenant_tx(self._sessions, self._tenant) as s:
            renders = [
                {
                    "render_id": str(r.id),
                    "status": r.status,
                    "renderer_version": r.renderer_version,
                    "unicode_version": r.unicode_version,
                    "tzdata_version": r.tzdata_version,
                    "options_hash": r.options_hash,
                    "head_seq": r.head_seq,
                    "head_hash": r.head_hash,
                    "seal_key": r.seal_storage_key,
                    "seal_version_id": r.seal_version_id,
                    "files": r.file_count,
                    "natives": r.native_count,
                    "natives_bytes": int(r.natives_bytes or 0),
                    "externals": [],
                }
                for r in (
                    await s.execute(
                        text(
                            "SELECT r.*, (SELECT sum(size_bytes) FROM render_natives n"
                            " WHERE n.render_id = r.id) AS natives_bytes FROM renders r"
                            " WHERE r.job_id = :j AND r.sealed_at IS NOT NULL ORDER BY r.id"
                        ),
                        {"j": self._job_id},
                    )
                ).all()
            ]
            for render in renders:
                render["externals"] = [
                    {
                        "ord": n.ord,
                        "sha256": n.sha256,
                        "size": n.size_bytes,
                        "file_ords": list(n.file_ords),
                    }
                    for n in (
                        await s.execute(
                            text(
                                "SELECT ord, sha256, size_bytes, file_ords FROM render_natives"
                                " WHERE render_id = :r ORDER BY ord"
                            ),
                            {"r": uuid.UUID(render["render_id"])},
                        )
                    ).all()
                ]
            gaps = [
                {
                    "evidence_object_id": str(g.evidence_object_id),
                    "owner_type": g.owner_type,
                    "owner_id": str(g.owner_id),
                    "unprotected_from": m.iso(g.unprotected_from),
                    "unprotected_until": m.iso(g.unprotected_until),
                    "outcome": g.outcome,
                }
                for g in (
                    await s.execute(
                        text(
                            "SELECT g.* FROM retention_gaps g JOIN evidence_objects e"
                            " ON e.id = g.evidence_object_id WHERE e.job_id = :j"
                            " ORDER BY g.unprotected_from, g.id"
                        ),
                        {"j": self._job_id},
                    )
                ).all()
            ]
            head = (
                await s.execute(
                    text(
                        "SELECT last_seq, last_hash FROM custody_chain_heads WHERE stream_id = :t"
                    ),
                    {"t": self._tenant},
                )
            ).first()
        snap: dict[str, Any] = {
            "renders": renders,
            "retention_gaps": gaps,
            # the job's own collected evidence as of now (retention moves on; productions and
            # report files of the job are not its evidence)
            "evidence": await self._evidence(),
            "audit_head": None if head is None else {"seq": head.last_seq, "hash": head.last_hash},
            "lock": await self._lock_settings(),
        }
        snap["digest"] = canonical_hash(snap)
        return snap

    async def _lock_settings(self) -> dict[str, Any]:
        bucket = self._settings.s3_evidence_bucket
        lock = await self._s3.get_object_lock_configuration(Bucket=bucket)
        versioning = await self._s3.get_bucket_versioning(Bucket=bucket)
        rule = lock.get("ObjectLockConfiguration", {}).get("Rule", {}).get("DefaultRetention", {})
        return {
            "bucket": bucket,
            "object_lock": lock.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled"),
            "mode": rule.get("Mode"),
            "default_retention_days": rule.get("Days"),
            "versioning": versioning.get("Status"),
        }

    async def _evidence(self) -> dict[str, Any]:
        async with tenant_tx(self._sessions, self._tenant) as s:
            r = (
                await s.execute(
                    text(
                        "SELECT min(retain_until) AS lo, max(retain_until) AS hi, count(*) AS n,"
                        " count(*) FILTER (WHERE state <> 'complete') AS not_complete"
                        " FROM evidence_objects WHERE job_id = :j AND render_id IS NULL"
                        " AND report_id IS NULL AND kind NOT IN ('production', 'report')"
                    ),
                    {"j": self._job_id},
                )
            ).one()
        return {
            "retain_until_min": m.iso(r.lo),
            "retain_until_max": m.iso(r.hi),
            "objects": int(r.n),
            "not_complete": int(r.not_complete),
        }

    async def _normalizer_versions(self) -> list[str]:
        async with tenant_tx(self._sessions, self._tenant) as s:
            return sorted(
                (
                    await s.execute(
                        text(
                            "SELECT DISTINCT d.normalizer_version FROM job_items ji"
                            " JOIN item_derivations d ON d.item_id = ji.item_id WHERE ji.job_id = :j"
                        ),
                        {"j": self._job_id},
                    )
                ).scalars()
            )

    async def _pauses_db(self) -> list[dict[str, Any]]:
        async with tenant_tx(self._sessions, self._tenant) as s:
            return [
                {
                    "reason": p.reason,
                    "connection_id": str(p.connection_id),
                    "resumed": p.resumed_at is not None,
                }
                for p in (
                    await s.execute(
                        text(
                            "SELECT reason, connection_id, resumed_at FROM job_pauses"
                            " WHERE job_id = :j ORDER BY paused_at, id"
                        ),
                        {"j": self._job_id},
                    )
                ).all()
            ]

    async def _audit_events(self, head: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """Tenant audit events about the job (render and report requests ...) up to the audit head
        of the snapshot."""
        if head is None:
            return []
        async with tenant_tx(self._sessions, self._tenant) as s:
            return [
                {
                    "seq": a.seq,
                    "event_type": a.event_type,
                    "actor": a.actor,
                    "at": m.iso(a.created_at),
                }
                for a in (
                    await s.execute(
                        text(
                            "SELECT seq, event_type, actor, created_at FROM custody_events"
                            " WHERE stream_id = :t AND seq <= :h AND payload->>'job_id' = :j"
                            " ORDER BY seq LIMIT :n"
                        ),
                        {"t": self._tenant, "h": head["seq"], "j": str(self._job_id), "n": m.CAP},
                    )
                ).all()
            ]

    # ------------------------------------------------------------------ everything
    async def build(
        self, sink: Sink, *, snapshot: Mapping[str, Any] | None = None,
        identity: Mapping[str, Any] | None = None, image_digest: str | None = None,
    ) -> BuiltReport:  # fmt: skip
        """Every file once, in order (units, observations, renders, report.json), each streamed
        through ``sink``. ``build_files`` is the same with re-creatable streams (storage)."""

        async def once(
            name: str, make: Callable[[], AsyncIterator[bytes]], rows: Callable[[], int | None]
        ) -> None:
            await sink(name, make())

        return await self.build_files(
            once, snapshot=snapshot, identity=identity, image_digest=image_digest
        )

    async def build_files(
        self, sink: FileSink, *, snapshot: Mapping[str, Any] | None = None,
        identity: Mapping[str, Any] | None = None, image_digest: str | None = None,
    ) -> BuiltReport:  # fmt: skip
        """The report's files in order. For each, ``sink`` gets its name, a factory of its bytes
        (each call rebuilds them from the start: the writer may hash a file before storing it) and
        its row count once a stream was consumed in full. Everything a file states comes from the
        stream consumed LAST, so a rebuilt file never counts twice."""
        job = await self.job()
        snap = dict(snapshot) if snapshot is not None else await self.snapshot()
        verification = await self.verification(job)
        archive_backed = job["connection_source"] == "slack_export"
        chain = await self.chain_pass()
        database = await self.database_pass(archive_backed)
        divergences, stated = await self.resolve(chain.units.differing(database), archive_backed)
        divergent = {d.subject for d in divergences}
        divergences.extend(self._job_divergences(job, chain, await self._pauses_db()))
        zone = (chain.started.payload.get("unit_day_zone") if chain.started else None) or (
            m.ZONE_NOT_RECORDED
        )

        state: dict[str, Any] = {}

        def units_stream() -> AsyncIterator[bytes]:
            state["units"], state["units_digest"] = _UnitsPass(), m.JsonlDigest("units.jsonl")
            return self.units_file(
                zone=zone, archive_backed=archive_backed, stated=stated, divergent=divergent,
                acc=state["units"], digest=state["units_digest"],
            )  # fmt: skip

        def observations_stream() -> AsyncIterator[bytes]:
            state["obs"] = (Counter(), Counter(), m.Capped())
            state["obs_digest"] = m.JsonlDigest("observations.jsonl")
            counts, by_reason, capped = state["obs"]
            return self.observations_file(counts, by_reason, capped, state["obs_digest"])

        def renders_stream() -> AsyncIterator[bytes]:
            state["renders_digest"] = m.JsonlDigest("renders.jsonl")
            return self.renders_file(snap.get("renders", []), state["renders_digest"])

        await sink("units.jsonl", units_stream, lambda: state["units_digest"].rows)
        await sink("observations.jsonl", observations_stream, lambda: state["obs_digest"].rows)
        await sink("renders.jsonl", renders_stream, lambda: state["renders_digest"].rows)
        units: _UnitsPass = state["units"]
        obs_counts, by_reason, obs_capped = state["obs"]
        files = [state["units_digest"], state["obs_digest"], state["renders_digest"]]
        conversations = units.conversations_capped
        conversations_file: dict[str, Any] | None = None
        if units.conversations.count > m.CAP:
            # above the cap the report names the remainder by file: the file must exist (§1, §4.6)
            def conversations_stream() -> AsyncIterator[bytes]:
                state["conv"] = m.Capped()
                state["conv_digest"] = m.JsonlDigest("conversations.jsonl")
                return self.conversations_file(
                    zone=zone, archive_backed=archive_backed, stated=stated, divergent=divergent,
                    capped=state["conv"], digest=state["conv_digest"],
                )  # fmt: skip

            await sink(
                "conversations.jsonl", conversations_stream, lambda: state["conv_digest"].rows
            )
            files.append(state["conv_digest"])
            conversations, conversations_file = state["conv"], state["conv_digest"].record()

        evidence = dict(snap["evidence"])
        exceptions: dict[str, Any] = {
            "units": units.exceptions.record(state["units_digest"].record()),
            "observations_capped": obs_capped.record(state["obs_digest"].record()),
            "unavailable_files_by_reason": [
                {"reason": reason, "count": n}
                for (kind, reason), n in sorted(by_reason.items())
                if kind == "file_unavailable"
            ],
            "evidence_not_complete": evidence["not_complete"],
        }
        job_facts = {
            k: (m.iso(v) if isinstance(v, datetime) else (str(v) if isinstance(v, uuid.UUID) else v))
            for k, v in job.items()
            if k in ("id", "tenant_id", "client_id", "matter_id", "workspace_id", "connection_id",
                     "status", "rerun_of", "created_at", "started_at", "finished_at", "sealed_at")
        }  # fmt: skip
        inputs = m.ReportInputs(
            job=job_facts, chain=chain, verification=verification, snapshot=snap,
            access={"connection_source": job["connection_source"], "plan_tier": job["plan_tier"],
                    "granted_scopes": list(job["granted_scopes"] or []),
                    "blind_spots": job["blind_spots"]},
            unit_status_counts=units.status_counts, recon_counts=units.recon_counts,
            totals=units.totals, exceptions=exceptions, observation_counts=obs_counts,
            pauses_db=await self._pauses_db(), divergences=divergences,
            normalizer_versions=await self._normalizer_versions(),
            versions={"report_renderer": REPORT_RENDERER_VERSION,
                      "unicode": unicodedata.unidata_version,
                      "pdf_toolchain": dict(identity or {}).get("toolchain_id"),
                      "worker_image": image_digest or m.UNKNOWN},
            audit_events=await self._audit_events(snap.get("audit_head")),
            files=files, evidence=evidence, identity=dict(identity or {}),
            conversations=m.conversations_section(conversations, conversations_file),
        )  # fmt: skip
        document = await asyncio.to_thread(m.report_document, inputs)
        body = await asyncio.to_thread(canonical_json, document)
        page = await asyncio.to_thread(report_html, document)

        async def one(data: bytes) -> AsyncIterator[bytes]:
            yield data

        await sink("report.json", lambda: one(body), lambda: None)
        await sink("report.html", lambda: one(page), lambda: None)
        return BuiltReport(body, document, files, divergences, bool(document["job"]["clean"]))

    @staticmethod
    def _job_divergences(
        job: Mapping[str, Any], chain: m.ChainFold, pauses: Sequence[Mapping[str, Any]]
    ) -> list[m.Divergence]:
        out: list[m.Divergence] = []
        if chain.final_status is not None and chain.final_status != job["status"]:
            out.append(m.Divergence("job_status_differs", "job", chain.final_status, job["status"]))
        if chain.final is None:
            out.append(m.Divergence("job_final_event_missing", "job", None, job["status"]))
        if chain.duplicate_starts or chain.duplicate_finals:
            out.append(
                m.Divergence(
                    "duplicate_job_event",
                    "job",
                    {
                        "job_started": chain.duplicate_starts + 1,
                        "final": chain.duplicate_finals + 1,
                    },
                    None,
                )
            )
        chain_pauses = [
            {
                "reason": p.reason,
                "connection_id": p.connection_id,
                "resumed": p.resumed_at is not None,
            }
            for p in chain.pauses
        ]
        if chain_pauses != list(pauses):
            out.append(m.Divergence("pauses_differ", "job", chain_pauses, list(pauses)))
        return out
