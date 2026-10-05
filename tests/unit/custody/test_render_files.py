"""Render file batches in custody (ADR 0015 §14): the leaf, the batch root and the root over batches."""

from __future__ import annotations

from typing import Any

import pytest

from edisc_custody.merkle import EMPTY_ROOT
from edisc_custody.render_files import (
    FILE_FIELDS,
    FILE_FIELDS_1,
    NATIVE_FIELDS,
    RenderFileError,
    batches_root,
    files_root,
    has_natives,
    natives_root,
)


def record(ord: int, **overrides: Any) -> dict[str, Any]:  # noqa: A002 - the field's name
    base: dict[str, Any] = {
        "ord": ord, "name": f"C1_2026-01-0{ord}_part001of001.rsmf", "conversation_id": "C1",
        "day": "2026-01-01", "time_zone": "UTC", "part": 1, "parts": 1, "version_id": f"v{ord}",
        "sha256": f"{ord:02x}" * 32, "size": 100 + ord, "source_hash": "ab" * 32,
        "event_collection_id": "e", "event_count": 3, "context_event_count": 0,
        "attachment_count": 0, "unavailable_count": 0, "external_count": 0,
    }  # fmt: skip
    return {**base, **overrides}


def native(ord: int, *file_ords: int, **overrides: Any) -> dict[str, Any]:  # noqa: A002
    sha = f"{ord + 0xA0:02x}" * 32
    base: dict[str, Any] = {
        "ord": ord, "sha256": sha, "size": 2 << 20, "storage_key": f"t/x/productions/r/natives/sha256/{sha}",
        "version_id": f"nv{ord}", "file_ords": list(file_ords),
    }  # fmt: skip
    return {**base, **overrides}


def test_every_recorded_field_is_in_the_root() -> None:
    files = [record(0), record(1)]
    root = files_root(files)
    for f in FILE_FIELDS:
        if f == "ord":
            continue
        changed = [record(0), record(1, **{f: "x" if isinstance(files[1][f], str) else 999})]
        assert files_root(changed) != root, f


def test_files_must_be_in_render_order_and_complete() -> None:
    with pytest.raises(RenderFileError, match="not after"):
        files_root([record(1), record(0)])
    with pytest.raises(RenderFileError, match="not after"):
        files_root([record(0), record(0)])
    partial = record(0)
    del partial["sha256"]
    with pytest.raises(RenderFileError, match="lacks"):
        files_root([partial])
    assert files_root([]) == EMPTY_ROOT


def test_the_root_over_batches_depends_on_order_and_count() -> None:
    a, b = files_root([record(0)]), files_root([record(1)])
    assert batches_root([a, b]) != batches_root([b, a])
    assert batches_root([a, b]) != batches_root([a, b, b])
    assert batches_root([]) == EMPTY_ROOT
    with pytest.raises(RenderFileError):
        batches_root(["abc"])


# ------------------------------------------------------------------ the verifier's render checks
def _chain(payloads: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    from edisc_custody.chain import GENESIS_HASH, EventRecord, compute_event_hash, hashed_fields

    out, prev = [], GENESIS_HASH
    for seq, (event_type, payload) in enumerate(payloads, start=1):
        fields = hashed_fields(
            tenant_id="t", stream_id="r", job_id=None, seq=seq, event_type=event_type,
            actor="system:render", item_id=None, payload=payload,
            created_at="2026-10-05T00:00:00.000000Z",
        )  # fmt: skip
        h = compute_event_hash(prev, fields)
        out.append(EventRecord(f"e{seq}", fields, prev, h))
        prev = h
    return out


def _verify(
    completed: dict[str, Any],
    batches: list[list[dict[str, Any]]],
    natives: list[list[dict[str, Any]]] | None = None,
    version: str = "1.2.0",
    batch_payloads: list[dict[str, Any]] | None = None,
) -> list[str]:
    """A render stream: render_started (``version``), the batches (with their natives from 1.3.0),
    render_completed. ``batch_payloads`` overrides fields of the batch events."""
    from edisc_custody.chain import ChainVerifier

    fields = FILE_FIELDS if has_natives(version) else FILE_FIELDS_1
    natives = natives or [[] for _ in batches]
    payloads: list[tuple[str, dict[str, Any]]] = [("render_started", {"renderer_version": version})]
    first = first_native = 0
    for i, (files, batch_natives) in enumerate(zip(batches, natives, strict=True)):
        payload: dict[str, Any] = {"batch": i, "first_ord": first, "file_count": len(files),
                                   "merkle_root": files_root(files, fields)}  # fmt: skip
        if has_natives(version):
            try:  # an inconsistent case under test gets its root from ``batch_payloads``
                root = natives_root(batch_natives, first_native)
            except RenderFileError:
                root = ""
            payload |= {
                "native_count": len(batch_natives),
                "first_native_ord": first_native,
                "natives_root": root,
            }
        payload |= (batch_payloads or [{}] * len(batches))[i]
        payloads.append(("render_files_batch", payload))
        first += len(files)
        first_native += len(batch_natives)
    payloads.append(("render_completed", completed))
    v = ChainVerifier("t", "r")
    events = _chain(payloads)
    v.add_event(events[0])
    for ev, files, batch_natives in zip(events[1:-1], batches, natives, strict=True):
        v.add_event(ev, files=files, natives=batch_natives)
    v.add_event(events[-1])
    return v.finish(require_seal=False).errors


def test_render_completed_totals_are_recomputed_from_the_batches() -> None:
    batches = [[record(0), record(1)], [record(2)]]
    roots = [files_root(b, FILE_FIELDS_1) for b in batches]
    good = {"file_count": 3, "batch_count": 2, "batches_root": batches_root(roots)}
    assert _verify(good, batches) == []
    assert any("counts 4 files" in e for e in _verify({**good, "file_count": 4}, batches))
    assert any("counts 1 batches" in e for e in _verify({**good, "batch_count": 1}, batches))
    swapped = {**good, "batches_root": batches_root(roots[::-1])}
    assert any("batches_root does not match" in e for e in _verify(swapped, batches))


def test_a_batch_that_skips_or_repeats_files_is_caught() -> None:
    from edisc_custody.chain import ChainVerifier

    files = [record(0), record(1)]
    payloads = [
        ("render_files_batch", {"batch": 0, "first_ord": 0, "file_count": 2, "merkle_root": files_root(files)}),
        ("render_files_batch", {"batch": 0, "first_ord": 0, "file_count": 2, "merkle_root": files_root(files)}),
    ]  # fmt: skip
    v = ChainVerifier("t", "r")
    for ev in _chain(payloads):
        v.add_event(ev, files=files)
    errors = v.finish(require_seal=False).errors
    assert any("render batch 0 where 1 was expected" in e for e in errors)
    assert any("starts at ord 0, expected 2" in e for e in errors)


# ------------------------------------------------------------------ renderer 1.3.0: leaves and natives
def test_the_file_leaf_follows_the_renderer_version() -> None:
    old = {k: v for k, v in record(0).items() if k != "external_count"}
    batches = [[old]]
    good = {
        "file_count": 1,
        "batch_count": 1,
        "batches_root": batches_root([files_root([old], FILE_FIELDS_1)]),
    }
    assert _verify(good, batches, version="1.2.0") == []  # renders before 1.3.0 keep the old leaf
    with pytest.raises(RenderFileError, match="external_count"):
        files_root([old])
    assert not has_natives("1.2.0") and has_natives("1.3.0") and has_natives("1.10.0")
    with pytest.raises(RenderFileError):
        has_natives("1.x")


def _native_render() -> tuple[
    dict[str, Any], list[list[dict[str, Any]]], list[list[dict[str, Any]]]
]:
    batches = [[record(0), record(1)], [record(2)]]
    natives = [[native(0, 0, 2), native(1, 1)], [native(2, 2)]]
    roots = [files_root(b) for b in batches]
    every = [n for batch in natives for n in batch]
    completed = {"file_count": 3, "batch_count": 2, "batches_root": batches_root(roots),
                 "native_count": 3, "natives_root": natives_root(every)}  # fmt: skip
    return completed, batches, natives


def test_natives_are_verified_batch_by_batch_and_in_total() -> None:
    completed, batches, natives = _native_render()
    assert _verify(completed, batches, natives, "1.3.0") == []
    # a native record altered after the fact
    altered = [[natives[0][0], {**natives[0][1], "size": 1}], natives[1]]
    errors = _verify(
        completed, batches, altered, "1.3.0", [{"natives_root": natives_root(natives[0])}, {}]
    )
    assert any("natives_root mismatch" in e for e in errors), errors
    # a native dropped from its batch
    errors = _verify(completed, batches, [natives[0][:1], natives[1]], "1.3.0",
                     [{"native_count": 2, "natives_root": natives_root(natives[0])}, {"first_native_ord": 2}])  # fmt: skip
    assert any("natives 2 from 0, found 1" in e for e in errors), errors
    # totals in render_completed
    assert any(
        "counts 2 natives" in e
        for e in _verify({**completed, "native_count": 2}, batches, natives, "1.3.0")
    )
    wrong = {**completed, "natives_root": natives_root(natives[0])}
    assert any(
        "render_completed natives_root" in e for e in _verify(wrong, batches, natives, "1.3.0")
    )


def test_a_native_must_be_first_referenced_in_its_own_batch_and_listed_once() -> None:
    completed, batches, _ = _native_render()
    late = [[native(0, 0), native(1, 2)], [native(2, 2)]]  # native 1 first referenced by file 2
    every = [n for b in late for n in b]
    errors = _verify({**completed, "natives_root": natives_root(every)}, batches, late, "1.3.0")
    assert any("not by a file of its batch" in e for e in errors), errors
    twice = [[native(0, 0), native(1, 1)], [native(2, 2, sha256=native(0, 0)["sha256"])]]
    every = [n for b in twice for n in b]
    errors = _verify({**completed, "natives_root": natives_root(every)}, batches, twice, "1.3.0")
    assert any("recorded twice" in e for e in errors), errors
    beyond = [[native(0, 0, 7), native(1, 1)], [native(2, 2)]]
    every = [n for b in beyond for n in b]
    errors = _verify({**completed, "natives_root": natives_root(every)}, batches, beyond, "1.3.0")
    assert any("referenced by file 7" in e for e in errors), errors


def test_natives_in_an_older_render_are_refused() -> None:
    old = {k: v for k, v in record(0).items() if k != "external_count"}
    root = batches_root([files_root([old], FILE_FIELDS_1)])
    errors = _verify(
        {"file_count": 1, "batch_count": 1, "batches_root": root},
        [[old]],
        [[native(0, 0)]],
        "1.2.0",
    )
    assert any("natives in a render of renderer 1.2.0" in e for e in errors), errors


def test_native_records_are_complete_ordered_and_referenced() -> None:
    assert natives_root([native(0, 0), native(1, 3)]) != natives_root([native(0, 0), native(1, 4)])
    for f in NATIVE_FIELDS:
        partial = native(0, 0)
        del partial[f]
        with pytest.raises(RenderFileError, match="lacks"):
            natives_root([partial])
    with pytest.raises(RenderFileError, match="where 0"):
        natives_root([native(1, 0)])
    for ords in ([], [2, 1], [1, 1]):
        with pytest.raises(RenderFileError, match="file_ords"):
            natives_root([native(0, *ords)])
