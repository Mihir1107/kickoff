"""Property tests over random dummy datasets (ADR 0015 + the M15 review requirements):

- reconciliation: every in-scope item of the job appears as exactly one primary event across all files,
  plus marked context events, checked here from the parsed bytes and against the oracle;
- every manifest validates against the vendored schema, and every file passes the structural checks;
- byte-identical output on repeated runs (fresh inputs, fresh renderer state);
- slices over 10,000 events split into parts that respect the cap, context included.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from edisc_connector_dummy.spec import DatasetSpec
from edisc_connectors_base.types import ThreadParentPolicy
from edisc_core.schemas import ARCHIVE_CAVEAT
from edisc_custody.merkle import batch_root
from edisc_renderers.rsmf import (
    FileAttachment,
    FileUnavailable,
    RenderOptions,
    RenderResult,
    render_job,
    slice_day,
)
from tests.unit.renderers.emlcheck import check_eml, custom
from tests.unit.renderers.oracle import OracleJob, build_job

ZONES = ("UTC", "America/New_York", "Asia/Kolkata", "Australia/Lord_Howe", "Pacific/Apia")
# around DST transitions in the zones above, and an ordinary week
START_DAYS = (date(2026, 1, 5), date(2026, 3, 7), date(2026, 4, 4), date(2026, 10, 31))


@st.composite
def jobs(draw: st.DrawFn) -> tuple[DatasetSpec, dict[str, object], RenderOptions]:
    days = draw(st.integers(1, 3))
    epoch = draw(st.integers(0, 2))
    n_days = days + epoch
    day_from = draw(st.integers(0, n_days - 1))
    day_to = draw(st.integers(day_from + 1, n_days))
    spec = DatasetSpec(
        seed=draw(st.integers(0, 1_000_000)),
        dialect=draw(st.sampled_from(["slack", "slack_history"])),
        conversations=draw(st.integers(1, 4)),
        days=days,
        messages_per_unit=draw(st.integers(12, 40)),
        start_day=draw(st.sampled_from(START_DAYS)),
        p_reply_prev_day=draw(st.sampled_from([0.06, 0.3])),
        p_edit=draw(st.sampled_from([0.06, 0.3])),
        p_delete=draw(st.sampled_from([0.03, 0.2])),
    )
    job_args: dict[str, object] = {
        "epoch": epoch,
        "day_from": day_from,
        "day_to": day_to,
        "policy": draw(st.sampled_from(list(ThreadParentPolicy))),
        "unavailable_every": draw(st.sampled_from([0, 2, 4])),
        "completeness_basis": draw(st.sampled_from(["source", "archive"])),
    }
    options = RenderOptions(
        include_context=draw(st.booleans()),
        time_zone=draw(st.sampled_from(ZONES)),
        cap=draw(st.sampled_from([2, 3, 5, 8, 13, 40, 10_000])),
    )
    return spec, job_args, options


def _render(oj: OracleJob, options: RenderOptions) -> RenderResult:
    return render_job(oj.job, oj.conversations, oj.messages, oj.identities, oj.files, options)


def _digests(oj: OracleJob, result: RenderResult) -> list[str]:
    out = []
    for f in result.files:
        h = hashlib.sha256()
        for chunk in f.stream(oj.opener):
            h.update(chunk)
        out.append(h.hexdigest())
    return out


def _expected_source_hash(oj: OracleJob, manifest: dict[str, object]) -> str:
    """Recompute `X-RSMF-SourceHash` from the ORACLE's items for the events in the file."""
    by_subject = {m.subject: m for m in oj.messages}
    leaves: dict[str, str] = {}
    for e in manifest["events"]:  # type: ignore[attr-defined]
        m = by_subject[custom(e)["edisc.source_item_id"][0]]
        for s in m.states:
            leaves[s.item.idempotency_key] = s.item.content_hash
        if m.reactions is not None:
            leaves[m.reactions.item.idempotency_key] = m.reactions.item.content_hash
        for fid in [] if m.current.deleted else m.current.file_ids:
            item = oj.files[fid].item
            leaves[item.idempotency_key] = item.content_hash
    return batch_root(leaves.items())


def check_render(oj: OracleJob, options: RenderOptions, result: RenderResult) -> list[str]:
    """Check every file of a render against the oracle; return the SHA-256 of each file's bytes."""
    zone = ZoneInfo(options.time_zone)
    expected = Counter(m.subject for m in oj.in_scope)
    in_scope = {m.subject: m for m in oj.in_scope}
    out_of_scope = {m.subject for m in oj.messages if not m.in_scope}
    primaries: Counter[str] = Counter()
    context_events = unavailable = 0
    parts_by_slice: dict[tuple[str, date], list[tuple[int, int]]] = defaultdict(list)

    digests = []
    for f in result.files:
        data = b"".join(f.stream(oj.opener))
        digests.append(hashlib.sha256(data).hexdigest())
        parsed = check_eml(data)
        events = parsed.manifest["events"]
        assert 1 <= len(events) <= options.cap
        assert parsed.headers["X-RSMF-IncludeContext"] == str(options.include_context).lower()
        assert parsed.headers["X-RSMF-SourceHash"] == _expected_source_hash(oj, parsed.manifest)
        assert (ARCHIVE_CAVEAT in parsed.text) == (oj.job.completeness_basis == "archive")
        n, m = map(int, parsed.headers["X-RSMF-Part"].split("/"))
        parts_by_slice[(f.conversation_id, f.day)].append((n, m))
        ids = {e["id"] for e in events}
        primary_ids = set()
        for e in events:
            c = custom(e)
            subject = c["edisc.source_item_id"][0]
            if "edisc.context" in c:
                assert options.include_context, "context events only with include_context"
                context_events += 1
                (marker,) = c["edisc.context"]
                if subject in out_of_scope:
                    assert marker == "thread_root_out_of_scope"
                    assert c["edisc.in_scope"] == ["false"]
                else:
                    assert marker == "thread_root_outside_file"
                    assert c["edisc.in_scope"] == ["true"]
                assert "parent" not in e
            else:
                assert subject in in_scope, "only in-scope items are primary events"
                assert c["edisc.in_scope"] == ["true"]
                message = in_scope[subject]
                assert slice_day(message.sent_at, zone) == f.day, "event outside its slice"
                assert c["edisc.idempotency_key"] == [message.current.item.idempotency_key]
                assert len(e.get("edits", [])) == len(message.states) - 1
                primaries[subject] += 1
                primary_ids.add(e["id"])
                root = message.current.thread_root
                if root is not None and root != message.ts:
                    if "parent" in e:
                        assert e["parent"] == root
                    else:
                        assert c["edisc.parent_not_rendered"] == [root]
                        assert not options.include_context or root not in {
                            x.ts
                            for x in oj.messages
                            if x.conversation_id == message.conversation_id
                        }
            for fid_value in c.get("edisc.file_unavailable", []):
                fid = fid_value.split(":")[0]
                assert isinstance(oj.files[fid], FileUnavailable)
                unavailable += 1
            for a in e.get("attachments", []):
                fid = a["id"].split("_")[0]
                outcome = oj.files[fid]
                if isinstance(outcome, FileAttachment):
                    assert parsed.zip.read(a["id"]) == oj.dataset.file_bytes(fid)
        for e in events:  # a context root is never also a primary of the same file
            if "edisc.context" in custom(e):
                assert e["id"] not in primary_ids
        assert ids  # never an empty file

    # THE reconciliation invariant, from the bytes: every in-scope item exactly once
    assert primaries == expected
    for parts in parts_by_slice.values():
        m = parts[0][1]
        assert sorted(parts) == [(n, m) for n in range(1, m + 1)]
    r = result.reconciliation
    assert r.items_in == r.events_out == len(oj.in_scope)
    assert r.context_events == context_events
    assert r.unavailable_attachments == unavailable
    assert r.files == len(result.files)
    assert r.slices == len(parts_by_slice)
    return digests


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(jobs())
def test_random_jobs_reconcile_validate_and_are_byte_identical(
    case: tuple[DatasetSpec, dict[str, object], RenderOptions],
) -> None:
    spec, job_args, options = case
    oj = build_job(spec, **job_args)  # type: ignore[arg-type]
    result = _render(oj, options)
    digests = check_render(oj, options, result)
    again = build_job(spec, **job_args)  # type: ignore[arg-type]
    second = _render(again, options)
    assert [f.name for f in second.files] == [f.name for f in result.files]
    assert _digests(again, second) == digests
    assert second.reconciliation == result.reconciliation


def _big_slice(
    seed: int, messages: int, include_context: bool, p_reply_same_day: float
) -> RenderResult:
    spec = DatasetSpec(
        seed=seed,
        conversations=1,
        days=1,
        messages_per_unit=messages,
        p_reply_same_day=p_reply_same_day,
    )
    options = RenderOptions(include_context=include_context)
    oj = build_job(spec)
    result = _render(oj, options)
    assert len(result.files) >= 2, "a slice over the cap must split"
    assert all(f.event_count <= 10_000 for f in result.files)
    digests = check_render(oj, options, result)
    again = build_job(spec)
    assert _digests(again, _render(again, options)) == digests
    return result


@pytest.mark.parametrize("include_context", [True, False])
def test_a_slice_over_the_cap_splits_with_threads_across_the_cut(include_context: bool) -> None:
    """10,050 events: part 1 is full; replies in part 2 whose roots are in part 1 get the root as
    context (or `edisc.parent_not_rendered` with include_context off)."""
    result = _big_slice(3, 10_050, include_context, 0.45)
    first, last = result.files[0], result.files[-1]
    assert (len(result.files), first.event_count, first.context_event_count) == (2, 10_000, 0)
    r = result.reconciliation
    if include_context:
        assert last.context_event_count > 0
        assert last.event_count == 50 + last.context_event_count
        assert r.parents_not_rendered == 0
    else:
        assert last.event_count == 50
        assert r.context_events == 0 and r.parents_not_rendered > 0


@settings(
    max_examples=1,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(
    seed=st.integers(0, 1_000_000),
    messages=st.integers(10_001, 12_500),
    include_context=st.booleans(),
    p_reply_same_day=st.sampled_from([0.2, 0.45]),
)
def test_random_slices_over_ten_thousand_events_split(
    seed: int, messages: int, include_context: bool, p_reply_same_day: float
) -> None:
    """One random large slice per run (each run draws a new one); threads cross the part boundary."""
    _big_slice(seed, messages, include_context, p_reply_same_day)
