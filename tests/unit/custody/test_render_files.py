"""Render file batches in custody (ADR 0015 §14): the leaf, the batch root and the root over batches."""

from __future__ import annotations

from typing import Any

import pytest

from edisc_custody.merkle import EMPTY_ROOT
from edisc_custody.render_files import FILE_FIELDS, RenderFileError, batches_root, files_root


def record(ord: int, **overrides: Any) -> dict[str, Any]:  # noqa: A002 - the field's name
    base: dict[str, Any] = {
        "ord": ord, "name": f"C1_2026-01-0{ord}_part001of001.rsmf", "conversation_id": "C1",
        "day": "2026-01-01", "time_zone": "UTC", "part": 1, "parts": 1, "version_id": f"v{ord}",
        "sha256": f"{ord:02x}" * 32, "size": 100 + ord, "source_hash": "ab" * 32,
        "event_collection_id": "e", "event_count": 3, "context_event_count": 0,
        "attachment_count": 0, "unavailable_count": 0,
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


def _verify(completed: dict[str, Any], batches: list[list[dict[str, Any]]]) -> list[str]:
    from edisc_custody.chain import ChainVerifier

    payloads: list[tuple[str, dict[str, Any]]] = [("render_started", {})]
    first = 0
    for i, files in enumerate(batches):
        payloads.append(
            ("render_files_batch", {"batch": i, "first_ord": first, "file_count": len(files),
                                    "merkle_root": files_root(files)})
        )  # fmt: skip
        first += len(files)
    payloads.append(("render_completed", completed))
    v = ChainVerifier("t", "r")
    for ev, files in zip(_chain(payloads), [None, *batches, None], strict=True):
        v.add_event(ev, files=files)
    return v.finish(require_seal=False).errors


def test_render_completed_totals_are_recomputed_from_the_batches() -> None:
    batches = [[record(0), record(1)], [record(2)]]
    roots = [files_root(b) for b in batches]
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
