"""The render reconciliation fails loudly on every way the files can disagree with the job."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import pytest

from edisc_connector_dummy.spec import DatasetSpec
from edisc_core.canonical import canonical_json
from edisc_renderers.rsmf import (
    Reconciler,
    ReconciliationError,
    RenderedFile,
    RenderOptions,
    SliceInput,
    render_slice,
    slice_day,
    subject_digest,
)
from edisc_renderers.rsmf.render import _root_of
from tests.unit.renderers.oracle import OracleJob, build_job

OPTIONS = RenderOptions(cap=8)


def _slice(oj: OracleJob, index: int = 0) -> SliceInput:
    """A slice of conversation 0 with several parts and context (cap 8)."""
    zone = OPTIONS.zone()
    conv = oj.conversations[0]
    days = sorted({slice_day(m.sent_at, zone) for m in oj.in_scope if m.conversation_id == conv.id})
    day = days[index]
    msgs = tuple(
        m for m in oj.in_scope if m.conversation_id == conv.id and slice_day(m.sent_at, zone) == day
    )
    known = {m.ts: m for m in oj.messages if m.conversation_id == conv.id}
    own = {m.ts for m in msgs}
    wanted = {r for m in msgs if (r := _root_of(m)) and r not in own}
    return SliceInput(
        job=oj.job,
        conversation=conv,
        day=day,
        messages=msgs,
        roots={r: known[r] for r in wanted if r in known},
        missing_roots=frozenset(r for r in wanted if r not in known),
        identities=oj.identities,
        files=oj.files,
    )


@pytest.fixture(scope="module")
def oj() -> OracleJob:
    return build_job(
        DatasetSpec(seed=11, conversations=1, days=2, messages_per_unit=30), day_from=1
    )


@pytest.fixture(scope="module")
def rendered(oj: OracleJob) -> tuple[SliceInput, list[RenderedFile]]:
    inp = _slice(oj)
    files = render_slice(inp, OPTIONS)
    assert len(files) > 2 and any(f.context_event_count for f in files)
    return inp, files


def _edit(f: RenderedFile, change: Callable[[dict[str, Any]], None]) -> RenderedFile:
    manifest = json.loads(f.manifest)
    change(manifest)
    return dataclasses.replace(
        f, manifest=canonical_json(manifest), event_count=len(manifest["events"])
    )


def _primary(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        e for e in manifest["events"] if not any(c["name"] == "edisc.context" for c in e["custom"])
    ]


def _set_custom(event: dict[str, Any], name: str, value: str | None) -> None:
    event["custom"] = [c for c in event["custom"] if c["name"] != name]
    if value is not None:
        event["custom"].append({"name": name, "value": value})


def test_clean_slice_reconciles(rendered: tuple[SliceInput, list[RenderedFile]]) -> None:
    inp, files = rendered
    r = Reconciler(OPTIONS)
    r.add_slice(inp, files)
    subjects = [m.subject for m in inp.messages]
    summary = r.finish(len(subjects), subject_digest(subjects))
    assert summary.items_in == summary.events_out == len(subjects)
    assert summary.context_events == sum(f.context_event_count for f in files)
    assert summary.as_payload()["items_in"] == len(subjects)


def _fails(inp: SliceInput, files: list[RenderedFile], match: str) -> None:
    with pytest.raises(ReconciliationError, match=match):
        Reconciler(OPTIONS).add_slice(inp, files)


def test_a_dropped_event_fails(rendered: tuple[SliceInput, list[RenderedFile]]) -> None:
    inp, files = rendered

    def drop(m: dict[str, Any]) -> None:
        m["events"].remove(_primary(m)[-1])

    _fails(inp, [_edit(files[0], drop), *files[1:]], "missing")


def test_a_dropped_file_fails(rendered: tuple[SliceInput, list[RenderedFile]]) -> None:
    inp, files = rendered
    _fails(inp, files[:-1], "parts are not")
    _fails(inp, [], "files for")


def test_an_event_rendered_twice_fails(rendered: tuple[SliceInput, list[RenderedFile]]) -> None:
    inp, files = rendered
    extra = _primary(json.loads(files[1].manifest))[0]

    def dup(m: dict[str, Any]) -> None:  # substitute: same count, one item twice, one missing
        m["events"][m["events"].index(_primary(m)[-1])] = extra

    _fails(inp, [_edit(files[0], dup), *files[1:]], "unexpected")


def test_an_out_of_scope_primary_fails(rendered: tuple[SliceInput, list[RenderedFile]]) -> None:
    inp, files = rendered

    def flip(m: dict[str, Any]) -> None:
        _set_custom(_primary(m)[0], "edisc.in_scope", "false")

    _fails(inp, [_edit(files[0], flip), *files[1:]], "out-of-scope item as a primary")


def test_an_unmarked_context_event_counts_as_a_duplicate(
    rendered: tuple[SliceInput, list[RenderedFile]],
) -> None:
    inp, files = rendered
    index = next(i for i, f in enumerate(files) if f.context_event_count)

    def unmark(m: dict[str, Any]) -> None:
        for e in m["events"]:
            _set_custom(e, "edisc.context", None)

    bad = list(files)
    bad[index] = dataclasses.replace(_edit(files[index], unmark), context_event_count=0)
    _fails(inp, bad, "unexpected|missing|out-of-scope")


def test_a_context_event_that_is_not_a_needed_root_fails(
    rendered: tuple[SliceInput, list[RenderedFile]],
) -> None:
    inp, files = rendered
    index = next(i for i, f in enumerate(files) if f.context_event_count)

    def orphan(m: dict[str, Any]) -> None:
        for e in m["events"]:
            e.pop("parent", None)

    _fails(inp, [*files[:index], _edit(files[index], orphan), *files[index + 1 :]], "not a needed")


def test_a_wrong_context_marker_fails(rendered: tuple[SliceInput, list[RenderedFile]]) -> None:
    inp, files = rendered
    index = next(i for i, f in enumerate(files) if f.context_event_count)

    swap = {
        "thread_root_out_of_scope": "thread_root_outside_file",
        "thread_root_outside_file": "thread_root_out_of_scope",
    }

    def remark(m: dict[str, Any]) -> None:
        for e in m["events"]:
            for c in e["custom"]:
                if c["name"] == "edisc.context":
                    c["value"] = swap[c["value"]]

    _fails(inp, [*files[:index], _edit(files[index], remark), *files[index + 1 :]], "contradicts")


def test_a_file_over_the_cap_fails(rendered: tuple[SliceInput, list[RenderedFile]]) -> None:
    inp, files = rendered
    with pytest.raises(ReconciliationError, match="cap"):
        Reconciler(dataclasses.replace(OPTIONS, cap=2)).add_slice(inp, files)


def test_context_with_include_context_off_fails(
    rendered: tuple[SliceInput, list[RenderedFile]],
) -> None:
    inp, files = rendered
    with pytest.raises(ReconciliationError, match="include_context off"):
        Reconciler(RenderOptions(include_context=False, cap=8)).add_slice(inp, files)


def test_a_slice_rendered_twice_fails(rendered: tuple[SliceInput, list[RenderedFile]]) -> None:
    inp, files = rendered
    r = Reconciler(OPTIONS)
    r.add_slice(inp, files)
    with pytest.raises(ReconciliationError, match="rendered twice"):
        r.add_slice(inp, files)


def test_finish_compares_count_and_digest_with_the_job(
    rendered: tuple[SliceInput, list[RenderedFile]],
) -> None:
    inp, files = rendered
    subjects = [m.subject for m in inp.messages]
    r = Reconciler(OPTIONS)
    r.add_slice(inp, files)
    with pytest.raises(ReconciliationError, match="items expected"):
        r.finish(len(subjects) + 1, subject_digest(subjects))
    # same count, one subject substituted: only the digest notices
    substituted = [*subjects[:-1], subjects[0]]
    with pytest.raises(ReconciliationError, match="differ"):
        r.finish(len(subjects), subject_digest(substituted))


def test_subject_digest_is_order_independent_and_multiset_sensitive() -> None:
    assert subject_digest(["a", "b", "c"]) == subject_digest(["c", "a", "b"])
    assert subject_digest(["a", "b"]) != subject_digest(["a", "a"])
    assert subject_digest([]) == "0" * 64
