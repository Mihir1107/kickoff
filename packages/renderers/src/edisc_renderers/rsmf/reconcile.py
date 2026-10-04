"""Render reconciliation: every in-scope message of the job appears as exactly ONE event across all
files of the render, plus marked context events. Mismatches raise `ReconciliationError`.

The check reads the manifests back from their canonical bytes, so it verifies what was written, not
the planner's bookkeeping. It holds O(slices) state, not O(items):
- exactly once within a slice: the slice's primary events, as a multiset of subjects, equal its input;
- exactly once across slices: each (conversation, day) is accepted once, and the renderer refuses
  messages outside their slice's bounds;
- nothing lost between the job and the slices: `finish` compares the count and an order-independent
  digest of the subjects with what the caller derived from the job's links.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from edisc_renderers.rsmf.model import ReconciliationError, RenderOptions, SliceInput

if TYPE_CHECKING:
    from edisc_renderers.rsmf.render import RenderedFile

_MOD = 1 << 256
_CONTEXT_MARKERS = frozenset({"thread_root_outside_file", "thread_root_out_of_scope"})


def subject_digest(subjects: Iterable[str]) -> str:
    """Order-independent multiset digest: sum of SHA-256(subject) mod 2^256, hex. A missing subject plus
    a duplicated one changes it, which counts alone would not catch."""
    total = 0
    for s in subjects:
        total = (total + int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest(), "big")) % _MOD
    return f"{total:064x}"


@dataclass(frozen=True)
class Reconciliation:
    """The render's reconciliation summary; goes into the render's custody stream (M15 step 4)."""

    items_in: int  # in-scope messages handed to the renderer
    events_out: int  # primary (non-context) events across all files
    context_events: int  # marked thread-context events (repeats allowed across files)
    context_events_out_of_scope: int  # of which the root is out of the job's scope
    edits: int  # earlier versions rendered as edits
    attachments: int  # attachment references to collected files
    unavailable_attachments: int  # attachment references to placeholders
    parents_not_rendered: int  # replies whose parent is recorded in custom instead
    files: int
    slices: int
    subject_digest: str

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def _custom(event: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for pair in event.get("custom", []):
        out.setdefault(pair["name"], []).append(pair["value"])
    return out


def _one(custom: dict[str, list[str]], name: str, where: str) -> str:
    values = custom.get(name, [])
    if len(values) != 1:
        raise ReconciliationError(f"{where}: expected one {name}, found {len(values)}")
    return values[0]


class Reconciler:
    def __init__(self, options: RenderOptions) -> None:
        self._options = options
        self._slices: set[tuple[str, date]] = set()
        self._items_in = 0
        self._events_out = 0
        self._context = 0
        self._context_oos = 0
        self._edits = 0
        self._attachments = 0
        self._unavailable = 0
        self._not_rendered = 0
        self._files = 0
        self._digest = 0

    def add_slice(self, inp: SliceInput, files: Sequence[RenderedFile]) -> None:
        key = (inp.conversation.id, inp.day)
        where = f"slice {inp.conversation.id}/{inp.day.isoformat()}"
        if key in self._slices:
            raise ReconciliationError(f"{where} rendered twice")
        self._slices.add(key)
        expected = Counter(m.subject for m in inp.messages)
        if bool(files) != bool(expected):
            raise ReconciliationError(f"{where}: {len(files)} files for {len(expected)} items")
        if sorted(f.part for f in files) != list(range(1, len(files) + 1)) or any(
            f.parts != len(files) for f in files
        ):
            raise ReconciliationError(f"{where}: parts are not 1..{len(files)}")

        primaries: Counter[str] = Counter()
        for f in files:
            manifest = json.loads(f.manifest)
            events = manifest["events"]
            fwhere = f"{where} part {f.part}"
            if len(events) > self._options.cap or len(events) != f.event_count:
                raise ReconciliationError(
                    f"{fwhere}: {len(events)} events (cap {self._options.cap})"
                )
            primary_ids: set[str] = set()
            parents: set[str] = set()
            context_ids: list[str] = []
            for e in events:
                c = _custom(e)
                ewhere = f"{fwhere} event {e.get('id')}"
                marker = c.get("edisc.context")
                in_scope = _one(c, "edisc.in_scope", ewhere)
                subject = _one(c, "edisc.source_item_id", ewhere)
                self._unavailable += len(c.get("edisc.file_unavailable", []))
                self._attachments += len(e.get("attachments", [])) - len(
                    c.get("edisc.file_unavailable", [])
                )
                if marker is None:
                    if in_scope != "true":
                        raise ReconciliationError(f"{ewhere}: out-of-scope item as a primary event")
                    primaries[subject] += 1
                    primary_ids.add(e["id"])
                    self._edits += len(e.get("edits", []))
                    self._not_rendered += len(c.get("edisc.parent_not_rendered", []))
                    if "parent" in e:
                        parents.add(e["parent"])
                else:
                    if not self._options.include_context:
                        raise ReconciliationError(
                            f"{ewhere}: context event with include_context off"
                        )
                    if len(marker) != 1 or marker[0] not in _CONTEXT_MARKERS:
                        raise ReconciliationError(f"{ewhere}: bad context marker {marker}")
                    if (marker[0] == "thread_root_out_of_scope") != (in_scope == "false"):
                        raise ReconciliationError(f"{ewhere}: context marker contradicts scope")
                    context_ids.append(e["id"])
                    self._context_oos += in_scope == "false"
            for cid in context_ids:
                if cid in primary_ids or cid not in parents:
                    raise ReconciliationError(f"{fwhere}: context event {cid} is not a needed root")
            if len(context_ids) != f.context_event_count:
                raise ReconciliationError(f"{fwhere}: context count differs from the header")
            self._context += len(context_ids)
            self._files += 1

        if primaries != expected:
            missing = sorted((expected - primaries).elements())[:5]
            extra = sorted((primaries - expected).elements())[:5]
            raise ReconciliationError(f"{where}: missing {missing}, unexpected {extra}")
        if any(n != 1 for n in primaries.values()):
            raise ReconciliationError(f"{where}: an item rendered more than once")
        self._items_in += sum(expected.values())
        self._events_out += sum(primaries.values())
        for subject in primaries:
            self._digest = (
                self._digest + int.from_bytes(hashlib.sha256(subject.encode()).digest(), "big")
            ) % _MOD

    def finish(self, expected_items: int, expected_digest: str) -> Reconciliation:
        """Compare with what the caller derived independently from the job (its in-scope links)."""
        digest = f"{self._digest:064x}"
        if not (self._items_in == self._events_out == expected_items):
            raise ReconciliationError(
                f"items expected {expected_items}, handed in {self._items_in}, "
                f"rendered {self._events_out}"
            )
        if digest != expected_digest:
            raise ReconciliationError("rendered subjects differ from the job's in-scope subjects")
        return Reconciliation(
            items_in=self._items_in,
            events_out=self._events_out,
            context_events=self._context,
            context_events_out_of_scope=self._context_oos,
            edits=self._edits,
            attachments=self._attachments,
            unavailable_attachments=self._unavailable,
            parents_not_rendered=self._not_rendered,
            files=self._files,
            slices=len(self._slices),
            subject_digest=digest,
        )
