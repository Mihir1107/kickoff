"""The S3 legal-hold flags of a hold (M18 §1.3, D2; ADR 0019): ``LegalHoldWorkflow`` on queue ``holds``.

The worker runs as the HOLD storage identity (``edisc-hold``: legal-hold flags, nothing else) for
the flags, and as the app identity only to anchor the tenant stream. The database hold governs from
the moment it commits; the flags are defence in depth (they stop version deletes even for root).

**Flagging (an active hold).** A pass walks the hold's scope in bounded keyset pages: for each job
of each covered matter (id order), its own objects (``(job_id, id)``), then the objects behind the
items it linked (``job_items`` primary key; an archive entry resolves to its archive), then its
renders' and reports' objects; for a client hold, then its exports' archives. A sealed job's own objects
and links never change: once read after the seal (``legal_hold_jobs``), later passes read only its
renders' and reports' objects. Per page:
``PutObjectLegalHold(ON)`` on every pinned version not yet recorded for this hold (twice is
harmless), then ONE transaction: the ``legal_hold_objects`` rows, the counts, the checkpoint (only
if it is still the one this page started from: a moved cursor is a no-op) and a
``audit.legal_hold_objects_flagged`` event (Merkle root over the page's rows). A version the store
does not have is an INCIDENT (event + alert, counted), never skipped silently. Progress is what the
store confirmed: ``objects_in_scope`` is known only when a pass finished.

**Clearing (a released hold).** Page by page over this hold's rows: a version some other ACTIVE hold
still covers (``evidence_held``) keeps its flag; any other is read first, and cleared only if the
store says ON. A flag that disagrees with the database (D2: not ON although recorded) is an
incident and is never "fixed" on the way. Each page in one transaction with
``audit.legal_hold_objects_cleared``.

Re-runs: ``flag-held-objects`` (maintenance, every 15 min) starts a pass for every active hold
whose last pass finished, so objects linked after placement (new jobs, renders, reports, dedup
hits) get their flag; until then the database hold already blocks destruction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_core.canonical import canonical_json
from edisc_core.ids import new_id
from edisc_core.settings import Settings
from edisc_custody.log import anchor_if_due, append
from edisc_custody.merkle import merkle_root
from edisc_db.session import tenant_tx

ACTOR = "system:legal-hold"
PAGE = 500
MISSING = frozenset({"NoSuchKey", "NoSuchVersion"})
NO_FLAG = frozenset({"NoSuchObjectLockConfiguration"})  # MinIO: never had a legal hold

Hook = Callable[[str], Awaitable[None]]


async def _no_hook(point: str) -> None:
    return None


@dataclass(frozen=True)
class Obj:
    evidence_id: uuid.UUID
    key: str
    version_id: str
    sha256: str


def _leaf(o: Obj) -> bytes:
    return canonical_json(
        {"evidence_id": str(o.evidence_id), "key": o.key, "version_id": o.version_id,
         "sha256": o.sha256}
    )  # fmt: skip


# a page of the scope: (objects, next cursor or None when the pass is complete)
RESOLVED = (
    " SELECT DISTINCT a.id, a.storage_key, a.version_id, a.sha256 FROM evidence_objects a"
    " WHERE a.id = ANY(:ids) AND a.state = 'complete' AND a.version_id IS NOT NULL"
)


class HoldFlagger:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        app_s3: S3Client,
        hold_s3: S3Client,
        settings: Settings,
        *,
        hook: Hook = _no_hook,
    ) -> None:
        self.sessions, self.app_s3, self.hold_s3 = sessions, app_s3, hold_s3
        self.settings, self.hook = settings, hook

    # ------------------------------------------------------------------ passes
    async def begin(self, tenant_id: uuid.UUID, hold_id: uuid.UUID) -> str:
        """Start a pass if none is under way: an active hold flags, a released one clears."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            h = (await s.execute(text(
                "SELECT released_at, flag_state FROM legal_holds WHERE id = :h FOR NO KEY UPDATE"),
                {"h": hold_id})).one()  # fmt: skip
            if h.released_at is None and h.flag_state in ("pending", "flagged"):
                await s.execute(text(
                    "UPDATE legal_holds SET flag_state = 'flagging', flag_cursor = CAST(:c AS jsonb)"
                    " WHERE id = :h"), {"h": hold_id, "c": json.dumps({"phase": "start"})})  # fmt: skip
                return "flagging"
            if h.released_at is not None and h.flag_state != "cleared":
                if h.flag_state != "clearing":
                    await s.execute(text(
                        "UPDATE legal_holds SET flag_state = 'clearing', flag_cursor = NULL WHERE id = :h"),
                        {"h": hold_id})  # fmt: skip
                return "clearing"
            return str(h.flag_state)

    async def step(self, tenant_id: uuid.UUID, hold_id: uuid.UUID) -> bool:
        """One page. True when the pass is complete (nothing more to do now)."""
        async with tenant_tx(self.sessions, tenant_id) as s:
            h = (await s.execute(text(
                "SELECT matter_id, client_id, released_at, flag_state, flag_cursor FROM legal_holds"
                " WHERE id = :h"), {"h": hold_id})).one()  # fmt: skip
        if h.flag_state == "flagging" and h.released_at is None:
            return await self._flag_page(tenant_id, hold_id, h)
        if h.flag_state == "clearing":
            return await self._clear_page(tenant_id, hold_id, h.flag_cursor)
        return True

    # ------------------------------------------------------------------ flagging
    async def _scope_page(
        self, s: AsyncSession, hold_id: uuid.UUID, h: Any
    ) -> tuple[list[Obj], dict[str, Any] | None, uuid.UUID | None]:
        """The next page of the scope from the cursor: (objects, next cursor, a sealed job whose
        links were read completely by this page). Each read is one keyset page on an index."""
        cur: dict[str, Any] = dict(h.flag_cursor or {"phase": "start"})
        matters = (
            [h.matter_id] if h.matter_id else list((await s.execute(text(
                "SELECT id FROM matters WHERE client_id = :c"), {"c": h.client_id})).scalars())
        )  # fmt: skip
        while True:
            phase = cur["phase"]
            if phase in ("start", "next_job"):
                job = (await s.execute(text(
                    "SELECT id, sealed_at FROM collection_jobs WHERE matter_id = ANY(:ms)"
                    " AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT 1"),
                    {"ms": matters, "after": cur.get("job")})).one_or_none()  # fmt: skip
                if job is None:
                    cur = {"phase": "exports"} if h.client_id else {"phase": "done"}
                    continue
                cur = {"phase": "job_objects", "job": str(job.id), "after": None,
                       "sealed": job.sealed_at is not None}  # fmt: skip
                if cur["sealed"] and (await s.execute(text(
                    "SELECT 1 FROM legal_hold_jobs WHERE hold_id = :h AND job_id = :j"),
                    {"h": hold_id, "j": job.id})).first():  # fmt: skip
                    # its own objects and its links were read after the seal: they never change
                    cur = {**cur, "phase": "job_productions"}
                continue
            if phase == "job_objects":
                rows = (await s.execute(text(
                    "SELECT id, coalesce(archive_evidence_id, id) AS target FROM evidence_objects"
                    " WHERE job_id = :j AND (CAST(:after AS uuid) IS NULL OR id > :after)"
                    " ORDER BY id LIMIT :n"),
                    {"j": cur["job"], "after": cur["after"], "n": PAGE})).all()  # fmt: skip
                nxt = (
                    {**cur, "after": str(rows[-1].id)}
                    if len(rows) == PAGE
                    else {**cur, "phase": "job_links", "after": None}
                )
                return await self._resolve(s, [r.target for r in rows]), nxt, None
            if phase == "job_links":
                rows = (await s.execute(text(
                    "SELECT ji.item_id, coalesce(x.archive_evidence_id, x.id) AS target"
                    " FROM job_items ji JOIN items i ON i.id = ji.item_id"
                    " JOIN evidence_objects x ON x.id = i.evidence_object_id"
                    " WHERE ji.job_id = :j AND (CAST(:after AS uuid) IS NULL OR ji.item_id > :after)"
                    " ORDER BY ji.item_id LIMIT :n"),
                    {"j": cur["job"], "after": cur["after"], "n": PAGE})).all()  # fmt: skip
                if len(rows) == PAGE:
                    return (
                        await self._resolve(s, [r.target for r in rows]),
                        {**cur, "after": str(rows[-1].item_id)},
                        None,
                    )
                sealed_job = uuid.UUID(cur["job"]) if cur["sealed"] else None
                nxt = {**cur, "phase": "job_productions", "after": None}
                return await self._resolve(s, [r.target for r in rows]), nxt, sealed_job
            if phase == "job_productions":
                rows = (await s.execute(text(
                    "SELECT x.id FROM evidence_objects x WHERE (x.render_id IN"
                    " (SELECT id FROM renders WHERE job_id = :j) OR x.report_id IN"
                    " (SELECT id FROM reports WHERE job_id = :j))"
                    " AND (CAST(:after AS uuid) IS NULL OR x.id > :after) ORDER BY x.id LIMIT :n"),
                    {"j": cur["job"], "after": cur["after"], "n": PAGE})).all()  # fmt: skip
                nxt = (
                    {**cur, "after": str(rows[-1].id)}
                    if len(rows) == PAGE
                    else {"phase": "next_job", "job": cur["job"]}
                )
                return await self._resolve(s, [r.id for r in rows]), nxt, None
            if phase == "exports":
                rows = (await s.execute(text(
                    "SELECT evidence_object_id AS id FROM slack_exports WHERE client_id = :c"
                    " AND evidence_object_id IS NOT NULL"
                    " AND (CAST(:after AS uuid) IS NULL OR evidence_object_id > :after)"
                    " ORDER BY evidence_object_id LIMIT :n"),
                    {"c": h.client_id, "after": cur.get("after"), "n": PAGE})).all()  # fmt: skip
                nxt = (
                    {"phase": "exports", "after": str(rows[-1].id)}
                    if len(rows) == PAGE
                    else {"phase": "done"}
                )
                return await self._resolve(s, [r.id for r in rows]), nxt, None
            return [], None, None  # done

    async def _resolve(self, s: AsyncSession, ids: list[uuid.UUID]) -> list[Obj]:
        if not ids:
            return []
        rows = (await s.execute(text(RESOLVED), {"ids": list(set(ids))})).all()
        return sorted(
            (Obj(r.id, r.storage_key, r.version_id, r.sha256) for r in rows),
            key=lambda o: o.evidence_id,
        )

    async def _flag_page(self, tenant_id: uuid.UUID, hold_id: uuid.UUID, h: Any) -> bool:
        async with tenant_tx(self.sessions, tenant_id) as s:
            objs, nxt, sealed_job = await self._scope_page(s, hold_id, h)
            have: set[uuid.UUID] = set(
                (await s.execute(text(
                    "SELECT evidence_object_id FROM legal_hold_objects WHERE hold_id = :h"
                    " AND evidence_object_id = ANY(:ids)"),
                    {"h": hold_id, "ids": [o.evidence_id for o in objs]})).scalars()
            )  # fmt: skip
        todo = [o for o in objs if o.evidence_id not in have]
        flagged, missing = [], []
        for o in todo:  # outside any transaction (CLAUDE.md: no transaction across I/O)
            try:
                await self.hold_s3.put_object_legal_hold(
                    Bucket=self.settings.s3_evidence_bucket, Key=o.key, VersionId=o.version_id,
                    LegalHold={"Status": "ON"},
                )  # fmt: skip
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in MISSING:
                    missing.append(o)
                    continue
                raise
            flagged.append(o)
        await self.hook("flags_set")
        async with tenant_tx(self.sessions, tenant_id) as s:
            moved: bool = (await s.execute(text(
                "SELECT flag_cursor IS DISTINCT FROM CAST(:c AS jsonb) OR flag_state <> 'flagging'"
                " OR released_at IS NOT NULL FROM legal_holds WHERE id = :h FOR NO KEY UPDATE"),
                {"h": hold_id, "c": json.dumps(h.flag_cursor)})).scalar_one()  # fmt: skip
            if moved:
                return False  # another executor recorded this page (or the hold was released)
            inserted: list[uuid.UUID] = list(
                (await s.execute(text(
                    "INSERT INTO legal_hold_objects (tenant_id, hold_id, evidence_object_id,"
                    " storage_key, version_id) SELECT :t, :h, u.e, u.k, u.v FROM unnest("
                    " CAST(:es AS uuid[]), CAST(:ks AS text[]), CAST(:vs AS text[])) AS u(e, k, v)"
                    " ON CONFLICT DO NOTHING RETURNING evidence_object_id"),
                    {"t": tenant_id, "h": hold_id, "es": [o.evidence_id for o in flagged],
                     "ks": [o.key for o in flagged], "vs": [o.version_id for o in flagged]})
                ).scalars()
            )  # fmt: skip
            done = nxt is None or nxt.get("phase") == "done"
            if sealed_job is not None:
                await s.execute(text(
                    "INSERT INTO legal_hold_jobs (tenant_id, hold_id, job_id) VALUES (:t, :h, :j)"
                    " ON CONFLICT DO NOTHING"), {"t": tenant_id, "h": hold_id, "j": sealed_job})  # fmt: skip
            await s.execute(text(
                "UPDATE legal_holds SET objects_flagged = objects_flagged + :n,"
                " flag_incidents = flag_incidents + :m, flag_cursor = CAST(:c AS jsonb) WHERE id = :h"),
                {"n": len(inserted), "m": len(missing), "h": hold_id,
                 "c": None if done else json.dumps(nxt)})  # fmt: skip
            if inserted:
                page = [o for o in flagged if o.evidence_id in set(inserted)]
                await append(
                    s, tenant_id=tenant_id, stream_id=tenant_id, actor=ACTOR,
                    event_type="audit.legal_hold_objects_flagged",
                    payload={"hold_id": str(hold_id), "count": len(page),
                             "merkle_root": merkle_root(_leaf(o) for o in page)},
                )  # fmt: skip
            for o in missing:
                await self._incident(s, tenant_id, hold_id, o, "version_missing")
            if done:
                await s.execute(text(
                    "UPDATE legal_holds SET flag_state = 'flagged', last_pass_completed_at = now(),"
                    " objects_in_scope = (SELECT count(*) FROM legal_hold_objects WHERE hold_id = :h)"
                    "   + flag_incidents WHERE id = :h"), {"h": hold_id})  # fmt: skip
        await anchor_if_due(self.sessions, self.app_s3, self.settings, tenant_id=tenant_id,
                            stream_id=tenant_id)  # fmt: skip
        return done

    async def _incident(
        self, s: AsyncSession, tenant_id: uuid.UUID, hold_id: uuid.UUID, o: Obj, kind: str
    ) -> None:
        await append(
            s, tenant_id=tenant_id, stream_id=tenant_id, actor=ACTOR,
            event_type="audit.legal_hold_flag_incident",
            payload={"hold_id": str(hold_id), "kind": kind, "evidence_id": str(o.evidence_id),
                     "key": o.key, "version_id": o.version_id},
        )  # fmt: skip
        await s.execute(
            text("INSERT INTO alerts (id, tenant_id, kind, message) VALUES (:i, :t, :k, :m)"),
            {"i": new_id(), "t": tenant_id, "k": "legal_hold_flag_incident",
             "m": f"legal hold {hold_id}: {kind} for evidence {o.evidence_id} ({o.key})"[:2000]},
        )  # fmt: skip

    # ------------------------------------------------------------------ clearing
    async def _clear_page(
        self, tenant_id: uuid.UUID, hold_id: uuid.UUID, cursor: dict[str, Any] | None
    ) -> bool:
        after = (cursor or {}).get("after")
        async with tenant_tx(self.sessions, tenant_id) as s:
            rows = (await s.execute(text(
                "SELECT o.evidence_object_id AS id, o.storage_key, o.version_id, e.sha256,"
                " o.clear_requested_at IS NOT NULL AS resumed,"
                " evidence_held(o.evidence_object_id) AS still_held FROM legal_hold_objects o"
                " JOIN evidence_objects e ON e.id = o.evidence_object_id"
                " WHERE o.hold_id = :h AND o.cleared_at IS NULL"
                " AND (CAST(:after AS uuid) IS NULL OR o.evidence_object_id > :after)"
                " ORDER BY o.evidence_object_id LIMIT :n"),
                {"h": hold_id, "after": after, "n": PAGE})).all()  # fmt: skip
            to_clear = [r.id for r in rows if not r.still_held]
            # the intent, committed BEFORE any flag is turned off (resume vs tampering, D2)
            await s.execute(text(
                "UPDATE legal_hold_objects SET clear_requested_at = now() WHERE hold_id = :h"
                " AND evidence_object_id = ANY(:ids) AND clear_requested_at IS NULL"),
                {"h": hold_id, "ids": to_clear})  # fmt: skip
        cleared, kept, disagree = [], [], []
        for r in rows:
            o = Obj(r.id, r.storage_key, r.version_id, r.sha256)
            if r.still_held:  # another active hold covers it: the flag stays
                kept.append(o)
                continue
            status = await self._flag_status(o)
            if status == "ON":
                await self.hold_s3.put_object_legal_hold(
                    Bucket=self.settings.s3_evidence_bucket, Key=o.key, VersionId=o.version_id,
                    LegalHold={"Status": "OFF"},
                )  # fmt: skip
                cleared.append(o)
            elif status == "OFF" and r.resumed:  # our own clearing, interrupted after the call
                cleared.append(o)
            else:  # D2: the store disagrees with the database: refuse, never "fix"
                disagree.append(o)
            await self.hook("flag_cleared")
        done = len(rows) < PAGE
        async with tenant_tx(self.sessions, tenant_id) as s:
            await s.execute(text("SELECT 1 FROM legal_holds WHERE id = :h FOR NO KEY UPDATE"),
                            {"h": hold_id})  # fmt: skip
            ids = [o.evidence_id for o in (*cleared, *kept, *disagree)]
            marked: set[uuid.UUID] = set((await s.execute(text(
                "UPDATE legal_hold_objects SET cleared_at = now() WHERE hold_id = :h"
                " AND evidence_object_id = ANY(:ids) AND cleared_at IS NULL"
                " RETURNING evidence_object_id"), {"h": hold_id, "ids": ids})).scalars())  # fmt: skip
            if ids and not marked:
                return False  # another executor recorded this page
            cleared = [o for o in cleared if o.evidence_id in marked]
            kept = [o for o in kept if o.evidence_id in marked]
            disagree = [o for o in disagree if o.evidence_id in marked]
            await s.execute(text(
                "UPDATE legal_holds SET objects_cleared = objects_cleared + :n,"
                " flag_incidents = flag_incidents + :m, flag_cursor = CAST(:c AS jsonb),"
                " flag_state = CASE WHEN :done THEN 'cleared' ELSE flag_state END WHERE id = :h"),
                {"n": len(cleared), "m": len(disagree), "h": hold_id, "done": done,
                 "c": None if done or not rows else json.dumps({"after": str(rows[-1].id)})})  # fmt: skip
            if cleared or kept:
                await append(
                    s, tenant_id=tenant_id, stream_id=tenant_id, actor=ACTOR,
                    event_type="audit.legal_hold_objects_cleared",
                    payload={"hold_id": str(hold_id), "cleared": len(cleared),
                             "kept_for_other_holds": len(kept),
                             "cleared_root": merkle_root(_leaf(o) for o in cleared),
                             "kept_root": merkle_root(_leaf(o) for o in kept)},
                )  # fmt: skip
            for o in disagree:
                await self._incident(s, tenant_id, hold_id, o, "flag_disagrees_with_database")
        await anchor_if_due(self.sessions, self.app_s3, self.settings, tenant_id=tenant_id,
                            stream_id=tenant_id)  # fmt: skip
        return done

    async def _flag_status(self, o: Obj) -> str:
        try:
            got = await self.hold_s3.get_object_legal_hold(
                Bucket=self.settings.s3_evidence_bucket, Key=o.key, VersionId=o.version_id
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in NO_FLAG:
                return "OFF"
            if code in MISSING:
                return "MISSING"
            raise
        return str(got["LegalHold"]["Status"])
