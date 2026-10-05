"""Hash-chain rules shared by the DB log and the standalone verifier. Pure: stdlib + edisc_core.canonical.

    event_hash = SHA-256( prev_hash_hex_ascii || canonical_json(hashed_fields) )

``hashed_fields`` = {tenant_id, stream_id, job_id, seq, event_type, actor, item_id, payload, created_at}
(RFC 8785). ``prev_hash`` of seq 1 is 64 ASCII zeros. Payloads are restricted to JSON values that
survive a Postgres JSONB round trip exactly: no floats, no NUL characters, ints within I-JSON range.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from edisc_core.canonical import canonical_json, to_jsonable
from edisc_core.time import format_utc
from edisc_custody.merkle import batch_root, leaf_hash, root_from_leaf_hashes
from edisc_custody.render_files import (
    RENDER_BATCH_EVENT,
    RENDER_COMPLETED,
    RENDER_LIFECYCLE,
    RENDER_STARTED,
    RenderFileError,
    batches_root,
    file_fields,
    files_root,
    has_natives,
    native_leaf_record,
    natives_root,
)

GENESIS_HASH = "0" * 64
ANCHOR_FORMAT = "edisc-anchor/1"

# Lifecycle actions get individual events and always trigger an anchor (ADR 0003).
LIFECYCLE_EVENTS = frozenset(
    {
        "job_started",
        "job_retried",
        "job_failed",
        "job_finished",
        "job_cancelled",
        "unit_failed",
        "activity_retried",
        "report_generated",
        "evidence_verified",
        "evidence_recovered",
        "connection_created",
        "connection_validated",
        "connection_revoked",
        "custodian_merged",
        "custodian_split",
        *RENDER_LIFECYCLE,
    }
)
BATCH_EVENT = "items_collected"


class PayloadError(ValueError):
    pass


def _check_payload(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        raise PayloadError(f"floats are not allowed in custody payloads ({path})")
    if isinstance(value, str) and "\x00" in value:
        raise PayloadError(f"NUL characters are not allowed in custody payloads ({path})")
    if isinstance(value, dict):
        for k, v in value.items():
            _check_payload(k, f"{path}.<key>")
            _check_payload(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_payload(v, f"{path}[{i}]")


def normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = to_jsonable(payload)
    if not isinstance(value, dict):
        raise PayloadError("payload must be an object")
    _check_payload(value)
    return value


def hashed_fields(
    *,
    tenant_id: uuid.UUID | str,
    stream_id: uuid.UUID | str,
    job_id: uuid.UUID | str | None,
    seq: int,
    event_type: str,
    actor: str,
    item_id: uuid.UUID | str | None,
    payload: Mapping[str, Any],
    created_at: datetime | str,
) -> dict[str, Any]:
    return {
        "tenant_id": str(tenant_id),
        "stream_id": str(stream_id),
        "job_id": None if job_id is None else str(job_id),
        "seq": seq,
        "event_type": event_type,
        "actor": actor,
        "item_id": None if item_id is None else str(item_id),
        "payload": normalize_payload(payload),
        "created_at": created_at if isinstance(created_at, str) else format_utc(created_at),
    }


def compute_event_hash(prev_hash: str, fields: Mapping[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + canonical_json(fields)).hexdigest()


def anchor_document(*, tenant_id: str, stream_id: str, seq: int, event_hash: str) -> bytes:
    """Deterministic anchor body: the same head always produces byte-identical anchors (idempotent)."""
    return canonical_json(
        {
            "format": ANCHOR_FORMAT,
            "tenant_id": tenant_id,
            "stream_id": stream_id,
            "seq": seq,
            "event_hash": event_hash,
        }
    )


# ------------------------------------------------------------------ verification (shared)
@dataclass(frozen=True)
class EventRecord:
    """One stored custody event, as loaded from the DB or an exported package."""

    id: str
    fields: dict[str, Any]  # hashed_fields shape
    prev_hash: str
    event_hash: str

    @property
    def seq(self) -> int:
        return int(self.fields["seq"])

    @property
    def event_type(self) -> str:
        return str(self.fields["event_type"])


@dataclass(frozen=True)
class Anchor:
    """One WORM object version found under the stream's anchor prefix."""

    key: str
    version_id: str
    body: bytes


@dataclass
class VerificationReport:
    tenant_id: str
    stream_id: str
    events: int = 0
    head_hash: str = GENESIS_HASH
    batches_checked: int = 0
    items_checked: int = 0
    files_checked: int = 0
    anchors_checked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def fail(self, message: str) -> None:
        self.errors.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tenant_id": self.tenant_id,
            "stream_id": self.stream_id,
            "events": self.events,
            "head_hash": self.head_hash,
            "batches_checked": self.batches_checked,
            "items_checked": self.items_checked,
            "files_checked": self.files_checked,
            "anchors_checked": self.anchors_checked,
            "errors": self.errors,
        }


class ChainVerifier:
    """Streaming verifier used by both the DB ``verify_chain`` and the standalone ``edisc-verify``.

    Feed anchors first (small), then events in seq order, each batch event with its linked items.
    Memory is O(anchors + one batch), independent of chain length.

    Checks: gapless seq from 1; stream/tenant ids; prev_hash links; recomputed event hashes; every batch
    Merkle root and item count recomputed from item rows; every anchor version agrees with the event
    at its seq; no anchor points past the head; hidden anchors (delete markers) fail; a finalized
    stream must be sealed at its head. Render streams (ADR 0015 §14): every ``render_files_batch`` root,
    count, index and first ``ord`` recomputed from its file records, and ``render_completed``'s file
    count, batch count and root over the batch roots recomputed from the batches before it. From
    renderer 1.3.0 (the version ``render_started`` records, which also picks the file leaf) every
    batch's ``natives_root``, count and first native ``ord`` are recomputed from its native records,
    each native is first referenced by a file of its own batch and recorded once, and
    ``render_completed``'s native count and root over every native record are recomputed (§20).
    """

    def __init__(self, tenant_id: str, stream_id: str) -> None:
        self.report = VerificationReport(tenant_id=tenant_id, stream_id=stream_id)
        self._expected_seq = 1
        self._prev = GENESIS_HASH
        self._anchors: dict[int, list[tuple[str, str]]] = {}
        self._anchored_ok: set[int] = set()
        self._halted = False
        self._render_roots: list[str] = []
        self._render_files = 0
        self._renderer_version: str | None = None  # from render_started
        self._native_leaves: list[bytes] = []  # leaf hashes, 32 bytes per native
        self._native_shas: set[str] = set()
        self._native_max_file_ord = -1

    # -- anchors
    def add_anchor(self, anchor: Anchor) -> None:
        r = self.report
        r.anchors_checked += 1
        where = f"anchor {anchor.key} (version {anchor.version_id})"
        try:
            doc = json.loads(anchor.body)
        except ValueError:
            r.fail(f"{where}: not valid JSON")
            return
        if not isinstance(doc, dict) or doc.get("format") != ANCHOR_FORMAT:
            r.fail(f"{where}: unknown anchor format")
            return
        if doc.get("stream_id") != r.stream_id or doc.get("tenant_id") != r.tenant_id:
            r.fail(f"{where}: belongs to another stream or tenant")
            return
        seq, event_hash = doc.get("seq"), doc.get("event_hash")
        if not isinstance(seq, int) or seq < 1 or not isinstance(event_hash, str):
            r.fail(f"{where}: malformed seq/event_hash")
            return
        self._anchors.setdefault(seq, []).append((where, event_hash))

    def add_hidden_anchor(self, key: str, version_id: str) -> None:
        self.report.fail(
            f"anchor {key} has a delete marker (version {version_id}): someone tried to hide it"
        )

    # -- events
    def add_event(
        self,
        ev: EventRecord,
        batch_items: Iterable[tuple[str, str]] | None = None,
        files: Iterable[Mapping[str, Any]] | None = None,
        natives: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        """``batch_items``: the linked items of a collection batch; ``files``: the file records of a
        ``render_files_batch``, in render order; ``natives``: that batch's native records, in order."""
        r = self.report
        if self._halted:
            return
        if ev.seq != self._expected_seq:
            r.fail(f"seq gap: expected {self._expected_seq}, found {ev.seq}")
            self._halted = True
            return
        self._expected_seq += 1
        r.events += 1
        if ev.fields.get("stream_id") != r.stream_id or ev.fields.get("tenant_id") != r.tenant_id:
            r.fail(f"seq {ev.seq}: event belongs to another stream or tenant")
        if ev.prev_hash != self._prev:
            r.fail(f"seq {ev.seq}: prev_hash does not link to seq {ev.seq - 1}")
        try:
            recomputed = compute_event_hash(ev.prev_hash, ev.fields)
        except (ValueError, TypeError) as exc:
            r.fail(f"seq {ev.seq}: cannot canonicalize event: {exc}")
            recomputed = ""
        if recomputed != ev.event_hash:
            r.fail(f"seq {ev.seq}: event_hash mismatch (content altered)")
        self._prev = ev.event_hash
        r.head_hash = ev.event_hash
        if ev.event_type == BATCH_EVENT:
            self._check_batch(ev, list(batch_items or ()))
        elif ev.event_type == RENDER_STARTED:
            version = ev.fields.get("payload", {}).get("renderer_version")
            self._renderer_version = version if isinstance(version, str) else None
        elif ev.event_type == RENDER_BATCH_EVENT:
            self._check_render_batch(ev, list(files or ()), list(natives or ()))
        elif ev.event_type == RENDER_COMPLETED:
            self._check_render_completed(ev)
        for where, anchored_hash in self._anchors.get(ev.seq, ()):
            if anchored_hash == ev.event_hash:
                self._anchored_ok.add(ev.seq)
            else:
                r.fail(f"{where}: seq {ev.seq} disagrees with the WORM anchor (chain rewritten)")

    def _check_batch(self, ev: EventRecord, pairs: list[tuple[str, str]]) -> None:
        r = self.report
        r.batches_checked += 1
        r.items_checked += len(pairs)
        payload = ev.fields.get("payload", {})
        if payload.get("item_count") != len(pairs):
            r.fail(
                f"seq {ev.seq}: batch item_count {payload.get('item_count')} != {len(pairs)} linked items"
            )
        try:
            root = batch_root(pairs)
        except ValueError as exc:
            r.fail(f"seq {ev.seq}: {exc}")
            return
        if root != payload.get("merkle_root"):
            r.fail(
                f"seq {ev.seq}: batch Merkle root mismatch (item rows altered, added or removed)"
            )

    def _natives_on(self) -> bool:
        try:
            return has_natives(self._renderer_version)
        except RenderFileError as exc:
            self.report.fail(f"render_started: {exc}")
            return False

    def _check_render_batch(
        self, ev: EventRecord, files: list[Mapping[str, Any]], natives: list[Mapping[str, Any]]
    ) -> None:
        r = self.report
        r.batches_checked += 1
        r.files_checked += len(files)
        payload = ev.fields.get("payload", {})
        index = len(self._render_roots)
        if payload.get("batch") != index:
            r.fail(f"seq {ev.seq}: render batch {payload.get('batch')} where {index} was expected")
        if payload.get("file_count") != len(files):
            r.fail(
                f"seq {ev.seq}: render batch file_count {payload.get('file_count')} != {len(files)} files"
            )
        if payload.get("first_ord") != self._render_files:
            r.fail(
                f"seq {ev.seq}: render batch starts at ord {payload.get('first_ord')}, expected"
                f" {self._render_files} (files missing or repeated)"
            )
        if files and files[0].get("ord") != self._render_files:
            r.fail(
                f"seq {ev.seq}: first file has ord {files[0].get('ord')}, expected {self._render_files}"
            )
        try:
            root = files_root(files, file_fields(self._renderer_version))
        except RenderFileError as exc:
            r.fail(f"seq {ev.seq}: {exc}")
            root = ""
        if root != payload.get("merkle_root"):
            r.fail(
                f"seq {ev.seq}: render batch Merkle root mismatch (file records altered, added or removed)"
            )
        if self._natives_on():
            self._check_batch_natives(ev, payload, natives, self._render_files, len(files))
        elif natives or "natives_root" in payload:
            r.fail(f"seq {ev.seq}: natives in a render of renderer {self._renderer_version}")
        self._render_roots.append(str(payload.get("merkle_root")))
        self._render_files += len(files)

    def _check_batch_natives(
        self,
        ev: EventRecord,
        payload: Mapping[str, Any],
        natives: list[Mapping[str, Any]],
        first_file: int,
        file_count: int,
    ) -> None:
        r = self.report
        first = len(self._native_leaves)
        if payload.get("native_count") != len(natives) or payload.get("first_native_ord") != first:
            r.fail(
                f"seq {ev.seq}: render batch natives {payload.get('native_count')} from"
                f" {payload.get('first_native_ord')}, found {len(natives)} from {first}"
            )
        try:
            root = natives_root(natives, first)
        except RenderFileError as exc:
            r.fail(f"seq {ev.seq}: {exc}")
            root = ""
        if root != payload.get("natives_root"):
            r.fail(
                f"seq {ev.seq}: render batch natives_root mismatch (native records altered, added or"
                " removed)"
            )
        for n in natives:
            try:
                view = native_leaf_record(n)
            except RenderFileError:
                continue  # reported with the root above
            ords = view["file_ords"]
            if not first_file <= ords[0] < first_file + file_count:
                r.fail(
                    f"seq {ev.seq}: native {view['sha256']} is first referenced by file {ords[0]},"
                    " not by a file of its batch"
                )
            if view["sha256"] in self._native_shas:
                r.fail(f"seq {ev.seq}: native {view['sha256']} recorded twice")
            self._native_shas.add(view["sha256"])
            self._native_max_file_ord = max(self._native_max_file_ord, ords[-1])
            self._native_leaves.append(leaf_hash(canonical_json(view)))

    def _check_render_completed(self, ev: EventRecord) -> None:
        r = self.report
        payload = ev.fields.get("payload", {})
        if payload.get("file_count") != self._render_files:
            r.fail(
                f"seq {ev.seq}: render_completed counts {payload.get('file_count')} files, the batches"
                f" hold {self._render_files}"
            )
        if payload.get("batch_count") != len(self._render_roots):
            r.fail(
                f"seq {ev.seq}: render_completed counts {payload.get('batch_count')} batches, found"
                f" {len(self._render_roots)}"
            )
        try:
            root = batches_root(self._render_roots)
        except RenderFileError as exc:
            r.fail(f"seq {ev.seq}: {exc}")
            return
        if root != payload.get("batches_root"):
            r.fail(f"seq {ev.seq}: render_completed batches_root does not match the batch roots")
        if not self._natives_on():
            return
        total = root_from_leaf_hashes(self._native_leaves).hex()
        if payload.get("native_count") != len(self._native_leaves):
            r.fail(
                f"seq {ev.seq}: render_completed counts {payload.get('native_count')} natives, the"
                f" batches hold {len(self._native_leaves)}"
            )
        if payload.get("natives_root") != total:
            r.fail(f"seq {ev.seq}: render_completed natives_root does not match the native records")
        if self._native_max_file_ord >= self._render_files:
            r.fail(
                f"seq {ev.seq}: a native is referenced by file {self._native_max_file_ord}, the render"
                f" has {self._render_files} files"
            )

    # -- end
    def finish(
        self, *, require_seal: bool, expected_head: tuple[int, str] | None = None
    ) -> VerificationReport:
        r = self.report
        head_seq = self._expected_seq - 1
        for seq, entries in sorted(self._anchors.items()):
            if seq > head_seq:
                for where, _ in entries:
                    r.fail(
                        f"{where}: anchors seq {seq} but the chain ends at {head_seq} (events deleted)"
                    )
        if expected_head is not None and expected_head != (head_seq, r.head_hash):
            r.fail(
                f"chain head record {expected_head} does not match events ({head_seq}, {r.head_hash})"
            )
        if require_seal and head_seq and head_seq not in self._anchored_ok:
            r.fail(f"stream is finalized but head seq {head_seq} has no matching WORM seal")
        return r


def anchor_key(tenant_id: str, stream_id: str, seq: int) -> str:
    return f"{anchor_prefix(tenant_id, stream_id)}{seq:016d}.json"


def anchor_prefix(tenant_id: str, stream_id: str) -> str:
    return f"custody-anchors/{tenant_id}/{stream_id}/"
