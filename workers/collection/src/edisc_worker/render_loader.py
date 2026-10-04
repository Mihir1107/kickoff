"""Render inputs from a sealed job's items and derivations (ADR 0015 §1, M15 step 3).

The loader feeds the pure renderer one slice at a time:

- **What is read:** the job's linked message items, through their latest derivation, plus what they
  reference: earlier versions (edits), the latest reaction snapshot, files and their availability, and
  identity snapshots. Everything is taken as of the job's finish time.
- **Query by id** (CLAUDE.md): link ids are read per conversation from `job_items` (by unit key), and
  items by id. Days and types are filtered in Python, never by joining links to items for a day.
- **Verified inputs:** before a slice is handed to the renderer, every page or archive entry behind
  one of its items is read by its pinned VersionId. Its SHA-256 and size must equal the registry,
  and every item's sub-document at `json_path` must re-hash to `items.raw_hash`. File bytes are
  streamed by pinned version and checked by the renderer as they pass. Any mismatch raises, so
  nothing corrupted enters a production.
- **Thread roots:** a referenced root is either linked to the job (handed in as context) or declared
  missing. The renderer refuses anything else.
- **Memory:** one conversation's link index (subject, ts, version, scope) plus one slice of items.
  Pages are read one at a time; file bytes are never held whole.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from types_aiobotocore_s3 import S3Client

from edisc_connector_slack_export.archive_access import open_archive_entry
from edisc_core.canonical import canonical_hash
from edisc_core.jsonpath import JsonPathError, resolve
from edisc_core.schemas import ItemType, JobStatus
from edisc_core.settings import Settings
from edisc_db.session import tenant_tx
from edisc_evidence.writer import EvidenceWriter
from edisc_renderers.rsmf import (
    ConversationInfo,
    FileAttachment,
    FileOutcome,
    FileUnavailable,
    Identity,
    ItemRef,
    JobInfo,
    Message,
    MessageState,
    Reactions,
    RenderOptions,
    SliceInput,
    slice_day,
    subject_digest,
)
from edisc_renderers.rsmf.model import AsyncFileOpener

ID_CHUNK = 5_000
_EXPORT_KINDS = {
    "channel": "public_channel",
    "group": "private_channel",
    "dm": "im",
    "mpim": "mpim",
}


class RenderRefusedError(RuntimeError):
    """The job cannot be rendered (not finished and sealed)."""


class RenderInputIntegrityError(RuntimeError):
    """Stored evidence or registry rows disagree with what was recorded. Always an incident."""


@dataclass(frozen=True)
class LoadedJob:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    matter_id: uuid.UUID
    matter_retention_until: datetime
    finished_at: datetime
    status: JobStatus
    info: JobInfo
    conversations: tuple[str, ...]
    expected_items: int  # in-scope message subjects linked to the job
    expected_digest: str  # subject_digest of the same, computed by the database


@dataclass
class _Indexed:
    """One message subject of a conversation, as linked to the job."""

    source_item_id: str
    ts: str
    sent_at: datetime
    max_version: int
    in_scope: bool


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def _chunks[T](values: Sequence[T], size: int = ID_CHUNK) -> Iterable[Sequence[T]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


class RenderLoader:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        s3: S3Client,
        settings: Settings,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        options: RenderOptions,
    ) -> None:
        self._sessions, self._s3, self._settings = sessions, s3, settings
        self._tenant, self._job_id, self._options = tenant_id, job_id, options
        self._writer = EvidenceWriter(sessions, s3, settings)
        self._verified: set[uuid.UUID] = set()  # evidence objects verified during this render
        self._unverified: list[Any] = []  # item rows read since the last verification
        self._job: LoadedJob | None = None
        self._source = "slack"
        self._connection_id: uuid.UUID | None = None

    @property
    def verified_objects(self) -> int:
        return len(self._verified)

    # ------------------------------------------------------------------ the job
    async def load_job(self) -> LoadedJob:
        async with tenant_tx(self._sessions, self._tenant) as s:
            job = (
                await s.execute(
                    text(
                        "SELECT j.id, j.matter_id, j.status, j.sealed_at, j.finished_at, j.connector_version,"
                        " j.connection_id, c.source AS connection_source, m.retention_until"
                        " FROM collection_jobs j JOIN connections c ON c.id = j.connection_id"
                        " JOIN matters m ON m.id = j.matter_id WHERE j.id = :j"
                    ),
                    {"j": self._job_id},
                )
            ).one_or_none()
            if job is None:
                raise RenderRefusedError(f"job {self._job_id} not found")
            status = JobStatus(job.status)
            if not status.is_terminal or job.sealed_at is None or job.finished_at is None:
                raise RenderRefusedError(
                    f"job {self._job_id} is {status.value}, sealed={job.sealed_at is not None}: "
                    "only finished, sealed jobs are rendered"
                )
            conversations = tuple(
                r[0]
                for r in (
                    await s.execute(
                        text(
                            "SELECT DISTINCT conversation_id FROM work_units WHERE job_id = :j"
                            " AND kind = 'conversation_day' ORDER BY conversation_id"
                        ),
                        {"j": self._job_id},
                    )
                ).all()
            )
            versions = sorted(
                (
                    r[0]
                    for r in (
                        await s.execute(
                            text(
                                "SELECT DISTINCT d.normalizer_version FROM item_derivations d"
                                " WHERE d.item_id IN (SELECT item_id FROM job_items WHERE job_id = :j)"
                            ),
                            {"j": self._job_id},
                        )
                    ).all()
                ),
                key=_version_key,
            )
            # the expected side of the reconciliation, from the links alone (never from rendering)
            subjects = await s.stream(
                text(
                    "SELECT DISTINCT i.source_item_id FROM items i WHERE i.id IN"
                    " (SELECT item_id FROM job_items WHERE job_id = :j AND in_scope)"
                    " AND i.item_type = 'message'"
                ),
                {"j": self._job_id},
            )
            count, digest_ids = 0, []
            digest = subject_digest([])
            async for row in subjects:
                count += 1
                digest_ids.append(row[0])
                if len(digest_ids) >= ID_CHUNK:
                    digest = _add_digests(digest, subject_digest(digest_ids))
                    digest_ids = []
            digest = _add_digests(digest, subject_digest(digest_ids))
        self._connection_id = job.connection_id
        basis = "archive" if job.connection_source == "slack_export" else "source"
        self._job = LoadedJob(
            tenant_id=self._tenant,
            job_id=self._job_id,
            matter_id=job.matter_id,
            matter_retention_until=job.retention_until,
            finished_at=job.finished_at,
            status=status,
            info=JobInfo(
                job_id=self._job_id,
                connector_version=job.connector_version,
                normalizer_version=",".join(versions) or "none",
                completeness_basis=basis,  # type: ignore[arg-type]
            ),
            conversations=conversations,
            expected_items=count,
            expected_digest=digest,
        )
        return self._job

    # ------------------------------------------------------------------ slices
    async def slices(self) -> AsyncIterator[SliceInput]:
        """Every non-empty slice of the job, conversation by conversation, day by day."""
        job = self._job or await self.load_job()
        zone = self._options.zone()
        for conversation_id in job.conversations:
            index = await self._index(conversation_id)
            if not index:
                continue
            by_day: dict[date, list[str]] = defaultdict(list)
            for subject, entry in index.items():
                if entry.in_scope:
                    by_day[slice_day(entry.sent_at, zone)].append(subject)
            info = await self._conversation_info(conversation_id, index)
            for day in sorted(by_day):
                yield await self._slice(job, info, day, sorted(by_day[day]), index)

    async def _index(self, conversation_id: str) -> dict[str, _Indexed]:
        """The job's message links for one conversation, by id (never a day-wide join)."""
        async with tenant_tx(self._sessions, self._tenant) as s:
            units = [
                r[0]
                for r in (
                    await s.execute(
                        text(
                            "SELECT unit_key FROM work_units WHERE job_id = :j AND conversation_id = :c"
                            " AND kind = 'conversation_day'"
                        ),
                        {"j": self._job_id, "c": conversation_id},
                    )
                ).all()
            ]
            links = (
                await s.execute(
                    text(
                        "SELECT item_id, in_scope FROM job_items WHERE job_id = :j"
                        " AND unit_key = ANY(:u)"
                    ),
                    {"j": self._job_id, "u": units},
                )
            ).all()
            scope = {r.item_id: r.in_scope for r in links}
            out: dict[str, _Indexed] = {}
            for ids in _chunks(sorted(scope)):
                rows = (
                    await s.execute(
                        text(
                            "SELECT id, source, source_item_id, version, item_type, sent_at FROM items"
                            " WHERE id = ANY(:ids)"
                        ),
                        {"ids": list(ids)},
                    )
                ).all()
                for r in rows:
                    if r.item_type != ItemType.MESSAGE.value:
                        continue
                    self._source = r.source
                    ws_conv, _, ts = r.source_item_id.rpartition("/")
                    if not ws_conv.endswith("/" + conversation_id):
                        raise RenderInputIntegrityError(
                            f"{r.source_item_id} linked under conversation {conversation_id}"
                        )
                    entry = out.get(r.source_item_id)
                    if entry is None:
                        out[r.source_item_id] = _Indexed(
                            r.source_item_id, ts, r.sent_at, r.version, scope[r.id]
                        )
                    else:
                        if scope[r.id] != entry.in_scope:  # the normalizer decides scope by time
                            raise RenderInputIntegrityError(
                                f"{r.source_item_id}: versions linked with different scope"
                            )
                        entry.max_version = max(entry.max_version, r.version)
        return out

    async def _conversation_info(
        self, conversation_id: str, index: Mapping[str, _Indexed]
    ) -> ConversationInfo:
        workspace = next(iter(index)).rsplit("/", 2)[0]
        async with tenant_tx(self._sessions, self._tenant) as s:
            meta = (
                await s.execute(
                    text(
                        "SELECT x.kind, x.name FROM export_conversations x"
                        " JOIN slack_exports e ON e.id = x.export_id"
                        " WHERE e.connection_id = :c AND x.conversation_id = :v ORDER BY e.id LIMIT 1"
                    ),
                    {"c": self._connection_id, "v": conversation_id},
                )
            ).one_or_none()
            custodians = sorted(
                {
                    r[0]
                    for r in (
                        await s.execute(
                            text(
                                "SELECT DISTINCT cs.external_id FROM work_unit_scopes ws"
                                " JOIN work_units wu ON wu.job_id = ws.job_id AND wu.unit_key = ws.unit_key"
                                " JOIN collection_scopes cs ON cs.id = ws.scope_id"
                                " WHERE ws.job_id = :j AND wu.conversation_id = :v"
                                " AND cs.scope_type = 'custodian'"
                            ),
                            {"j": self._job_id, "v": conversation_id},
                        )
                    ).all()
                }
            )
        return ConversationInfo(
            id=conversation_id,
            slack_type=_EXPORT_KINDS.get(meta.kind) if meta else None,  # type: ignore[arg-type]
            workspace_id=workspace,
            name=meta.name if meta else None,
            custodian=custodians[0] if len(custodians) == 1 else None,
        )

    async def _slice(
        self,
        job: LoadedJob,
        info: ConversationInfo,
        day: date,
        subjects: list[str],
        index: Mapping[str, _Indexed],
    ) -> SliceInput:
        messages = await self._messages(job, [index[s] for s in subjects])
        prefix = f"{info.workspace_id}/{info.id}/"
        own = {m.ts for m in messages}
        wanted = sorted(
            {
                r
                for m in messages
                if (r := m.current.thread_root) is not None and r != m.ts and r not in own
            }
        )
        linked_roots = [index[prefix + r] for r in wanted if prefix + r in index]
        roots = {m.ts: m for m in await self._messages(job, linked_roots)}
        missing = frozenset(r for r in wanted if r not in roots)
        everything = [*messages, *roots.values()]
        files = await self._files(job, info.workspace_id, everything)
        users = sorted(
            {s.author for m in everything for s in m.states}
            | {u for m in everything if m.reactions for _, us in m.reactions.reactions for u in us}
        )
        identities = await self._identities(job, info.workspace_id, users)
        await self._verify_pending()  # nothing unverified reaches the renderer
        return SliceInput(
            job=job.info,
            conversation=info,
            day=day,
            messages=tuple(messages),
            roots=roots,
            missing_roots=missing,
            identities=identities,
            files=files,
        )

    # ------------------------------------------------------------------ items
    async def _items(
        self, s: AsyncSession, subjects: Sequence[str], item_type: str, job: LoadedJob
    ) -> dict[str, list[Any]]:
        """Every version of the subjects collected up to the job's finish, with its best derivation."""
        rows: list[Any] = []
        for chunk in _chunks(list(subjects)):
            rows += (
                await s.execute(
                    text(
                        "SELECT id, source_item_id, version, item_type, event_kind, content_hash, raw_hash,"
                        " idempotency_key, evidence_object_id, json_path, sent_at, collected_at FROM items"
                        " WHERE tenant_id = :t AND source = :s AND source_item_id = ANY(:ids)"
                        " AND collected_at <= :until"
                    ),
                    {
                        "t": self._tenant,
                        "s": self._source,
                        "ids": list(chunk),
                        "until": job.finished_at,
                    },
                )
            ).all()
        rows = [r for r in rows if r.item_type == item_type]
        derived: dict[uuid.UUID, tuple[tuple[int, ...], dict[str, Any]]] = {}
        for chunk in _chunks([r.id for r in rows]):
            for d in (
                await s.execute(
                    text(
                        "SELECT item_id, normalizer_version, derived, derived_hash FROM item_derivations"
                        " WHERE item_id = ANY(:ids)"
                    ),
                    {"ids": list(chunk)},
                )
            ).all():
                if canonical_hash(d.derived) != d.derived_hash:
                    raise RenderInputIntegrityError(f"derivation of item {d.item_id} was altered")
                key = _version_key(d.normalizer_version)
                if d.item_id not in derived or key > derived[d.item_id][0]:
                    derived[d.item_id] = (key, d.derived)
        out: dict[str, list[Any]] = defaultdict(list)
        for r in sorted(rows, key=lambda r: (r.source_item_id, r.version)):
            if r.id not in derived:
                raise RenderInputIntegrityError(f"item {r.id} has no derivation")
            out[r.source_item_id].append((r, derived[r.id][1]))
        self._unverified.extend(rows)  # verified after the transaction (no I/O inside it)
        return out

    async def _messages(self, job: LoadedJob, entries: Sequence[_Indexed]) -> list[Message]:
        if not entries:
            return []
        sids = [e.source_item_id for e in entries]
        async with tenant_tx(self._sessions, self._tenant) as s:
            versions = await self._items(s, sids, ItemType.MESSAGE.value, job)
            snapshots = await self._items(
                s, [f"{x}#reactions" for x in sids], ItemType.EVENT.value, job
            )
        out = []
        for e in entries:
            rows = [
                (r, d) for r, d in versions.get(e.source_item_id, []) if r.version <= e.max_version
            ]
            if not rows or rows[-1][0].version != e.max_version:
                raise RenderInputIntegrityError(f"{e.source_item_id}: linked version not found")
            states = tuple(
                MessageState(
                    item=ItemRef(r.source_item_id, r.version, r.idempotency_key, r.content_hash),
                    author=d["author_external_id"],
                    text=d["text"],
                    subtype=d["subtype"],
                    thread_root=d["thread_root"],
                    deleted=bool(d["deleted"]),
                    file_ids=tuple(d["file_ids"]),
                    edited_ts=d.get("edited_ts"),
                    deleted_ts=d.get("deleted_ts"),
                )
                for r, d in rows
            )
            reactions = None
            snap = snapshots.get(f"{e.source_item_id}#reactions")
            if snap:
                r, d = snap[-1]
                reactions = Reactions(
                    ItemRef(r.source_item_id, r.version, r.idempotency_key, r.content_hash),
                    tuple((name, tuple(users)) for name, users in d["reactions"]),
                )
            out.append(
                Message(
                    e.source_item_id.split("/")[1], e.ts, e.sent_at, e.in_scope, states, reactions
                )
            )
        return out

    async def _files(
        self, job: LoadedJob, workspace: str, messages: Sequence[Message]
    ) -> dict[str, FileOutcome]:
        fids = sorted({f for m in messages if not m.current.deleted for f in m.current.file_ids})
        if not fids:
            return {}
        async with tenant_tx(self._sessions, self._tenant) as s:
            items = await self._items(s, [f"{workspace}/file/{f}" for f in fids], "file", job)
            availability = await self._items(
                s, [f"{workspace}/file/{f}#availability" for f in fids], ItemType.EVENT.value, job
            )
            evidence = {
                r.id: r
                for chunk in _chunks([rows[-1][0].evidence_object_id for rows in items.values()])
                for r in (
                    await s.execute(
                        text(
                            "SELECT id, kind, state, sha256, size_bytes, version_id FROM evidence_objects"
                            " WHERE id = ANY(:ids)"
                        ),
                        {"ids": list(chunk)},
                    )
                ).all()
            }
        out: dict[str, FileOutcome] = {}
        for fid in fids:
            versions = items.get(f"{workspace}/file/{fid}")
            if versions:  # bytes collected at any time up to the job win over a later refusal
                r, d = versions[-1]
                ev = evidence[r.evidence_object_id]
                if ev.state != "complete" or not ev.version_id:
                    raise RenderInputIntegrityError(f"file {fid}: evidence not complete and pinned")
                if not (ev.sha256 == d["sha256"] == r.raw_hash) or ev.size_bytes != d["size"]:
                    raise RenderInputIntegrityError(f"file {fid}: registry and item disagree")
                out[fid] = FileAttachment(
                    fid, d["name"], ev.size_bytes, ev.sha256,
                    ItemRef(r.source_item_id, r.version, r.idempotency_key, r.content_hash),
                    handle=str(ev.id),
                )  # fmt: skip
                continue
            refused = [
                (r, d)
                for r, d in availability.get(f"{workspace}/file/{fid}#availability", [])
                if d.get("status") == "unavailable"
            ]
            if not refused:
                raise RenderInputIntegrityError(
                    f"file {fid}: neither collected nor recorded as refused"
                )
            r, d = refused[-1]
            out[fid] = FileUnavailable(
                fid, fid, str(d["reason"]),
                ItemRef(r.source_item_id, r.version, r.idempotency_key, r.content_hash),
            )  # fmt: skip
        return out

    async def _identities(
        self, job: LoadedJob, workspace: str, users: Sequence[str]
    ) -> dict[str, tuple[Identity, ...]]:
        if not users:
            return {}
        async with tenant_tx(self._sessions, self._tenant) as s:
            snaps = await self._items(
                s, [f"{workspace}/user/{u}#profile" for u in users], ItemType.EVENT.value, job
            )
        out: dict[str, tuple[Identity, ...]] = {}
        for rows in snaps.values():
            ordered = sorted(rows, key=lambda rd: (rd[0].collected_at, rd[0].version))
            uid = ordered[0][1]["user"]
            out[uid] = tuple(
                Identity(
                    user_id=uid,
                    effective_from=None if i == 0 else r.collected_at,
                    display_name=d.get("display_name"),
                    real_name=d.get("real_name"),
                    email=d.get("email"),
                    deactivated=d.get("deactivated"),
                )
                for i, (r, d) in enumerate(ordered)
            )
        return out

    # ------------------------------------------------------------------ verification
    async def _verify_pending(self) -> None:
        """Pages and archive entries behind the items read since the last call: pinned read, SHA-256
        and size against the registry, and every item's sub-document against its `raw_hash`. File
        items are checked by the renderer while their bytes stream into the zip. Runs outside any
        transaction; pages are read one at a time."""
        rows, self._unverified = self._unverified, []
        by_evidence: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for r in rows:
            by_evidence[r.evidence_object_id].append(r)
        todo = [e for e in by_evidence if e not in self._verified]
        if not todo:
            return
        async with tenant_tx(self._sessions, self._tenant) as s:
            registry = {
                r.id: r
                for chunk in _chunks(todo)
                for r in (
                    await s.execute(
                        text(
                            "SELECT id, kind, state, sha256, size_bytes, version_id FROM evidence_objects"
                            " WHERE id = ANY(:ids)"
                        ),
                        {"ids": list(chunk)},
                    )
                ).all()
            }
        for evidence_id in todo:
            ev = registry.get(evidence_id)
            if ev is None or ev.state != "complete" or not ev.version_id:
                raise RenderInputIntegrityError(
                    f"evidence {evidence_id} is not complete and pinned"
                )
            if ev.kind == "file":
                continue  # verified while streaming (size + SHA-256 against this registry row)
            data = await self._read_pinned(ev)
            document = json.loads(data)
            for item in by_evidence[evidence_id]:
                try:
                    node = resolve(document, item.json_path)
                except JsonPathError as exc:
                    raise RenderInputIntegrityError(f"item {item.id}: {exc}") from exc
                if canonical_hash(node) != item.raw_hash:
                    raise RenderInputIntegrityError(
                        f"item {item.id}: sub-document at {item.json_path} does not match raw_hash"
                    )
            self._verified.add(evidence_id)

    async def _read_pinned(self, ev: Any) -> bytes:
        if ev.kind == "archive_entry":
            chunks = open_archive_entry(
                self._sessions, self._s3, self._settings, tenant_id=self._tenant, evidence_id=ev.id
            )
        elif ev.kind == "page":
            chunks = self._writer.open(tenant_id=self._tenant, evidence_id=ev.id)
        else:
            raise RenderInputIntegrityError(f"evidence {ev.id}: unexpected kind {ev.kind}")
        digest, parts = hashlib.sha256(), []
        async for chunk in chunks:
            digest.update(chunk)
            parts.append(chunk)
        data = b"".join(parts)
        if (digest.hexdigest(), len(data)) != (ev.sha256, ev.size_bytes):
            raise RenderInputIntegrityError(
                f"evidence {ev.id}: pinned bytes hash {digest.hexdigest()}, recorded {ev.sha256}"
            )
        return data

    # ------------------------------------------------------------------ file bytes
    def opener(self) -> AsyncFileOpener:
        """Streams a file's evidence by pinned version; the renderer checks size and SHA-256."""

        async def open_file(f: FileAttachment) -> AsyncIterator[bytes]:
            evidence_id = uuid.UUID(f.handle)
            async with tenant_tx(self._sessions, self._tenant) as s:
                kind: str = (
                    await s.execute(
                        text("SELECT kind FROM evidence_objects WHERE id = :e"), {"e": evidence_id}
                    )
                ).scalar_one()
            if kind == "archive_entry":
                chunks = open_archive_entry(
                    self._sessions, self._s3, self._settings,
                    tenant_id=self._tenant, evidence_id=evidence_id,
                )  # fmt: skip
            else:
                chunks = self._writer.open(tenant_id=self._tenant, evidence_id=evidence_id)
            async for chunk in chunks:
                yield chunk

        return open_file


def _add_digests(a: str, b: str) -> str:
    return f"{(int(a, 16) + int(b, 16)) % (1 << 256):064x}"
