"""Mapping rules of ADR 0015 §2-§5 on small hand-built slices."""

from __future__ import annotations

import asyncio
import email
import email.policy
from datetime import date

import pytest

from edisc_core.schemas import ARCHIVE_CAVEAT
from edisc_renderers.rsmf import (
    EvidenceMismatchError,
    FileAttachment,
    Identity,
    JobInfo,
    RenderInputError,
    RenderOptions,
    ZipLimitError,
    render_slice,
)
from edisc_renderers.rsmf.slicing import slice_bounds, slice_day
from edisc_renderers.rsmf.version import RENDERER_VERSION
from edisc_renderers.rsmf.zipstream import ZipEntry, check_limits
from tests.unit.renderers.builders import (
    JOB,
    attachment,
    conv,
    msg,
    opener_for,
    ref,
    render,
    slice_input,
    ts_at,
    unavailable,
)
from tests.unit.renderers.emlcheck import custom

DAY = date(2026, 1, 5)


def _events(parsed: object) -> dict[str, dict[str, object]]:
    return {e["id"]: e for e in parsed.manifest["events"]}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("slack_type", "rsmf_type", "kind"),
    [
        ("im", "direct", "dm"),
        ("mpim", "direct", "mpim"),
        ("public_channel", "channel", "public"),
        ("private_channel", "channel", "private"),
    ],
)
def test_conversation_types(slack_type: str, rsmf_type: str, kind: str) -> None:
    [(_, parsed)] = render(
        slice_input(DAY, [msg("2026-01-05T09:00:00Z")], conversation=conv(slack_type))
    )
    (c,) = parsed.manifest["conversations"]
    assert c["type"] == rsmf_type and c["platform"] == "slack"
    cust = {x["name"]: x["value"] for x in c["custom"]}
    assert cust["slack.conversation_type"] == slack_type  # an mpim stays identifiable
    assert cust["slack.kind"] == kind and cust["slack.workspace_id"] == "T0TEST"
    assert c["participants"] == ["U1", "U2"]


def test_event_types_body_and_provenance() -> None:
    m1 = msg("2026-01-05T09:00:00.000100Z", subtype="channel_join", text="<@U1> has joined")
    m2 = msg("2026-01-05T09:01:00Z", subtype="channel_leave", text="<@U2> left", author="U2")
    m3 = msg("2026-01-05T09:02:00Z", subtype="pinned_item", text="pinned a message")
    m4 = msg("2026-01-05T09:03:00Z", subtype="bot_message", author="B1", text="beep")
    m5 = msg("2026-01-05T09:04:00Z", text="", files=("F1",))
    files = {"F1": attachment("F1", "a.txt", b"abc")}
    [(_, parsed)] = render(
        slice_input(DAY, [m1, m2, m3, m4, m5], files=files), blobs={"F1": b"abc"}
    )
    ev = _events(parsed)
    assert ev[m1.ts]["type"] == "join" and ev[m2.ts]["type"] == "leave"
    assert ev[m1.ts]["timestamp"] == "2026-01-05T09:00:00.000100Z"
    assert ev[m3.ts]["type"] == "unknown" and ev[m3.ts]["body"] == "pinned a message"
    assert custom(ev[m3.ts])["slack.subtype"] == ["pinned_item"]
    assert ev[m4.ts]["type"] == "message" and ev[m4.ts]["participant"] == "B1"
    assert "body" not in ev[m5.ts]  # empty optional fields are omitted
    c = custom(ev[m1.ts])
    item = m1.current.item
    assert c["edisc.idempotency_key"] == [item.idempotency_key]
    assert c["edisc.content_hash"] == [item.content_hash]
    assert c["edisc.source_item_id"] == [item.source_item_id]
    assert c["edisc.version"] == ["1"] and c["edisc.in_scope"] == ["true"]
    assert {p["id"] for p in parsed.manifest["participants"]} == {"U1", "U2", "B1"}


def test_edits_deletion_and_reactions() -> None:
    edited = msg(
        "2026-01-05T10:00:00Z",
        text="v3",
        edits=(("v1", None), ("v2", ts_at("2026-01-05T10:05:00Z"))),
        reactions=(("tada", ("U2", "U1")), ("eyes", ())),
    )
    gone = msg(
        "2026-01-05T11:00:00Z",
        deleted=True,
        edits=(("said this", None),),
        deleted_ts=ts_at("2026-01-05T12:00:00Z"),
    )
    [(_, parsed)] = render(slice_input(DAY, [edited, gone]))
    ev = _events(parsed)
    e = ev[edited.ts]
    assert e["body"] == "v3"
    assert e["edits"] == [
        {
            "participant": "U1",
            "previous": "v1",
            "new": "v2",
            "timestamp": "2026-01-05T10:05:00.000000Z",
        },
        {"participant": "U1", "previous": "v2", "new": "v3"},
    ]
    assert e["reactions"] == [
        {"value": "eyes", "count": 0},
        {"value": "tada", "count": 2, "participants": ["U1", "U2"]},
    ]
    c = custom(e)
    assert c["edisc.version"] == ["3"]
    assert c["edisc.prior_version_keys"] == [
        ",".join(s.item.idempotency_key for s in edited.states[:2])
    ]
    assert c["edisc.reactions.idempotency_key"] == [edited.reactions.item.idempotency_key]  # type: ignore[union-attr]
    d = ev[gone.ts]
    assert d["deleted"] is True and "body" not in d
    assert d["edits"] == [
        {
            "participant": "U1",
            "previous": "said this",
            "new": "",
            "timestamp": "2026-01-05T12:00:00.000000Z",
        }
    ]


def test_attachments_and_placeholders() -> None:
    data = b"\x00\x01binary" * 100
    m = msg("2026-01-05T09:00:00Z", files=("F1", "F2"))
    files = {
        "F1": attachment("F1", "Q3 report/v2:final.pdf", data),
        "F2": unavailable("F2", "gone.png", "expired_url"),
    }
    [(f, parsed)] = render(slice_input(DAY, [m], files=files), blobs={"F1": data})
    (e,) = parsed.manifest["events"]
    assert e["attachments"] == [
        {"id": "F1_Q3 report_v2_final.pdf", "display": "Q3 report/v2:final.pdf", "size": len(data)},
        {
            "id": "F2_gone.png.UNAVAILABLE.txt",
            "display": "gone.png",
            "size": parsed.zip.getinfo("F2_gone.png.UNAVAILABLE.txt").file_size,
        },
    ]
    assert parsed.zip.read("F1_Q3 report_v2_final.pdf") == data
    note = parsed.zip.read("F2_gone.png.UNAVAILABLE.txt").decode()
    assert "F2_gone.png" in note and "gone.png" in note
    assert "expired_url" in note and "F2" in note and files["F2"].item.idempotency_key in note
    assert custom(e)["edisc.file_unavailable"] == ["F2: expired_url"]
    assert (f.attachment_count, f.unavailable_count) == (2, 1)


def test_a_file_shared_by_two_events_is_one_zip_entry() -> None:
    a = msg("2026-01-05T09:00:00Z", files=("F1",))
    b = msg("2026-01-05T09:01:00Z", files=("F1",))
    [(f, parsed)] = render(
        slice_input(DAY, [a, b], files={"F1": attachment("F1", "x.txt", b"x")}),
        blobs={"F1": b"x"},
    )
    assert parsed.zip_names == ["F1_x.txt", "rsmf_manifest.json"]
    assert f.attachment_count == 1


def test_context_roots_are_marked_and_resolve_parents() -> None:
    old_root = msg("2026-01-04T09:00:00Z", text="root, yesterday")  # in scope, earlier slice
    oos_root = msg("2026-01-03T09:00:00Z", text="root, out of range", in_scope=False)
    r1 = msg("2026-01-05T09:00:00Z", root=old_root.ts)
    r2 = msg("2026-01-05T09:01:00Z", root=oos_root.ts)
    r3 = msg("2026-01-05T09:02:00Z", root=oos_root.ts)
    inp = slice_input(DAY, [r1, r2, r3], roots={old_root.ts: old_root, oos_root.ts: oos_root})
    [(f, parsed)] = render(inp)
    ev = _events(parsed)
    assert custom(ev[old_root.ts])["edisc.context"] == ["thread_root_outside_file"]
    assert custom(ev[oos_root.ts])["edisc.context"] == ["thread_root_out_of_scope"]
    assert custom(ev[oos_root.ts])["edisc.in_scope"] == ["false"]
    assert ev[r1.ts]["parent"] == old_root.ts and ev[r3.ts]["parent"] == oos_root.ts
    assert parsed.manifest["events"][0]["id"] == oos_root.ts  # context sorts by its own time
    assert (f.event_count, f.context_event_count) == (5, 2)
    assert parsed.headers["X-RSMF-BeginDate"] == "2026-01-03T09:00:00.000000Z"


def test_include_context_off_records_the_parent_instead() -> None:
    old_root = msg("2026-01-04T09:00:00Z")
    oos_root = msg("2026-01-03T09:00:00Z", in_scope=False)
    root_here = msg("2026-01-05T08:00:00Z")
    r1 = msg("2026-01-05T09:00:00Z", root=old_root.ts)
    r2 = msg("2026-01-05T09:01:00Z", root=oos_root.ts)
    r3 = msg("2026-01-05T09:02:00Z", root=root_here.ts)
    inp = slice_input(
        DAY, [root_here, r1, r2, r3], roots={old_root.ts: old_root, oos_root.ts: oos_root}
    )
    [(f, parsed)] = render(inp, RenderOptions(include_context=False))
    ev = _events(parsed)
    assert set(ev) == {root_here.ts, r1.ts, r2.ts, r3.ts}  # no out-of-scope item, no context
    for reply, root in ((r1, old_root), (r2, oos_root)):
        assert "parent" not in ev[reply.ts]
        assert custom(ev[reply.ts])["edisc.parent_not_rendered"] == [root.ts]
        assert custom(ev[reply.ts])["edisc.parent_not_rendered_reason"] == ["context_excluded"]
    assert ev[r3.ts]["parent"] == root_here.ts  # root in the file: the parent is kept
    assert f.context_event_count == 0
    assert parsed.headers["X-RSMF-IncludeContext"] == "false"


def test_a_root_the_job_does_not_hold_is_recorded_not_cut() -> None:
    reply = msg("2026-01-05T09:00:00Z", root="1767500000.000000")
    for include_context in (True, False):
        [(_, parsed)] = render(
            slice_input(DAY, [reply], missing=frozenset({"1767500000.000000"})),
            RenderOptions(include_context=include_context),
        )
        (e,) = parsed.manifest["events"]
        assert "parent" not in e
        assert custom(e)["edisc.parent_not_rendered"] == ["1767500000.000000"]
        assert custom(e)["edisc.parent_not_rendered_reason"] == ["not_collected"]


def test_parts_respect_the_cap_and_carry_context() -> None:
    root = msg("2026-01-05T01:00:00Z")
    replies = [msg(f"2026-01-05T02:{i:02d}:00Z", root=root.ts) for i in range(5)]
    files = render(slice_input(DAY, [root, *replies]), RenderOptions(cap=3))
    shapes = [(f.part, f.parts, f.event_count, f.context_event_count) for f, _ in files]
    assert shapes == [(1, 3, 3, 0), (2, 3, 3, 1), (3, 3, 2, 1)]
    for f, parsed in files:
        assert parsed.headers["X-RSMF-Part"] == f"{f.part}/3"
    names = [f.name for f, _ in files]
    assert names == [f"C1_2026-01-05_part00{n}of003.rsmf" for n in (1, 2, 3)]
    assert len({f.event_collection_id for f, _ in files}) == 3


def test_identity_in_force_at_the_slice_and_email_check() -> None:
    from edisc_core.time import parse_utc

    identities = {
        "U1": (
            Identity("U1", None, display_name="Alice", email="alice@example.test", team_id="T1"),
            Identity("U1", parse_utc("2026-01-06T00:00:00Z"), display_name="Alice B"),
        ),
        "U2": (Identity("U2", real_name="Bob Real", email="not-an-email", is_bot=True),),
    }
    for day, expected in ((DAY, "Alice"), (date(2026, 1, 6), "Alice B")):
        m = msg(f"{day.isoformat()}T09:00:00Z")
        [(_, parsed)] = render(slice_input(day, [m], identities=identities))
        people = {p["id"]: p for p in parsed.manifest["participants"]}
        assert people["U1"]["display"] == expected
        assert people["U2"]["display"] == "Bob Real" and "email" not in people["U2"]
        assert {"name": "slack.is_bot", "value": "true"} in people["U2"]["custom"]
        assert people["U1"]["account_id"] == "U1"


def test_custodian_and_participant_headers() -> None:
    identities = {"U2": (Identity("U2", display_name="Zoë ✓ 李"),)}
    c = conv(custodians=("U2",))
    [(_, parsed)] = render(
        slice_input(DAY, [msg("2026-01-05T09:00:00Z")], conversation=c, identities=identities)
    )
    assert parsed.manifest["conversations"][0]["custodian"] == "U2"
    assert parsed.headers["X-RSMF-Custodian"] == "Zoë ✓ 李"
    assert parsed.headers["X-RSMF-Participants"] == "U1, Zoë ✓ 李"


def test_archive_basis_puts_the_caveat_in_the_text_part() -> None:
    job = JobInfo(JOB.job_id, "slack_export/1", "0.1.0", "archive")
    [(_, parsed)] = render(slice_input(DAY, [msg("2026-01-05T09:00:00Z")], job=job))
    assert ARCHIVE_CAVEAT in parsed.text
    assert parsed.headers["X-RSMF-CompletenessBasis"] == "archive"


def test_non_ascii_channel_name_round_trips_through_headers() -> None:
    name = "équipe-שלום-\U0001f680" * 3
    [(_, parsed)] = render(
        slice_input(DAY, [msg("2026-01-05T09:00:00Z")], conversation=conv(name=name))
    )
    assert name in parsed.headers["Subject"]
    assert parsed.manifest["conversations"][0]["display"] == name


@pytest.mark.parametrize(
    ("zone", "day", "hours"),
    [
        ("UTC", date(2026, 3, 8), 24),
        ("America/New_York", date(2026, 3, 8), 23),
        ("America/New_York", date(2026, 11, 1), 25),
        ("Australia/Lord_Howe", date(2026, 4, 5), 24.5),
    ],
)
def test_dst_slices(zone: str, day: date, hours: float) -> None:
    z = RenderOptions(time_zone=zone).zone()
    start, end = slice_bounds(day, z)
    assert (end - start).total_seconds() == hours * 3600
    assert slice_day(start, z) == day and slice_day(end, z) != day


def test_messages_are_sliced_by_local_day() -> None:
    late = msg("2026-03-08T04:30:00Z")  # 23:30 EST on 7 March
    options = RenderOptions(time_zone="America/New_York")
    [(f, parsed)] = render(slice_input(date(2026, 3, 7), [late]), options)
    assert parsed.headers["X-RSMF-Slice"] == "conversation=C1; day=2026-03-07; tz=America/New_York"
    with pytest.raises(RenderInputError, match="outside slice"):
        render_slice(slice_input(date(2026, 3, 8), [late]), options)


def test_empty_slice_produces_no_file() -> None:
    assert render_slice(slice_input(DAY, []), RenderOptions()) == []


@pytest.mark.parametrize(
    "case",
    ["out_of_scope", "undeclared_root", "root_is_reply", "file_missing", "duplicate", "wrong_conv"],
)
def test_inconsistent_inputs_fail(case: str) -> None:
    m = msg("2026-01-05T09:00:00Z")
    if case == "out_of_scope":
        inp = slice_input(DAY, [msg("2026-01-05T09:00:00Z", in_scope=False)])
    elif case == "undeclared_root":
        inp = slice_input(DAY, [msg("2026-01-05T09:00:00Z", root="1767500000.000000")])
    elif case == "root_is_reply":
        fake = msg("2026-01-04T09:00:00Z", root="1767400000.000000")
        inp = slice_input(DAY, [msg("2026-01-05T09:00:00Z", root=fake.ts)], roots={fake.ts: fake})
    elif case == "file_missing":
        inp = slice_input(DAY, [msg("2026-01-05T09:00:00Z", files=("F9",))])
    elif case == "duplicate":
        inp = slice_input(DAY, [m, m])
    else:
        inp = slice_input(DAY, [msg("2026-01-05T09:00:00Z", conversation="C2")])
    with pytest.raises(RenderInputError):
        render_slice(inp, RenderOptions())


def test_evidence_bytes_are_verified_while_streaming() -> None:
    m = msg("2026-01-05T09:00:00Z", files=("F1",))
    [f] = render_slice(
        slice_input(DAY, [m], files={"F1": attachment("F1", "a.bin", b"right")}), RenderOptions()
    )
    for wrong in (b"wrong", b"righ", b"right!"):
        with pytest.raises(EvidenceMismatchError):
            b"".join(f.stream(opener_for({"F1": wrong})))


def test_zip64_sizes_never_fail_the_render_and_the_writer_refuses_them() -> None:
    """An attachment over 4 GiB leaves the zip (ADR 0015 §11); the writer's own check stays as the
    safety net and refuses a zip that would need ZIP64 before any byte."""
    big = FileAttachment("F1", "huge.bin", 5 * 2**30, "0" * 64, ref("T0TEST/file/F1"), "F1")
    [f] = render_slice(
        slice_input(DAY, [msg("2026-01-05T09:00:00Z", files=("F1",))], files={"F1": big}),
        RenderOptions(),
    )
    assert [a.file_id for a in f.externals] == ["F1"]
    with pytest.raises(ZipLimitError):
        check_limits([ZipEntry("F1_huge.bin", file=big)])


def test_options_are_validated() -> None:
    for bad in ({"cap": 1}, {"cap": 10_001}, {"time_zone": "Mars/Olympus"}):
        with pytest.raises(RenderInputError):
            RenderOptions(**bad)  # type: ignore[arg-type]


def test_the_eml_parses_with_the_standard_library() -> None:
    [f] = render_slice(slice_input(DAY, [msg("2026-01-05T09:00:00Z")]), RenderOptions())
    data = b"".join(f.stream(opener_for({})))
    parsed = email.message_from_bytes(data, policy=email.policy.default)
    assert parsed["Date"] == "Mon, 05 Jan 2026 09:00:00 +0000"
    assert parsed["X-RSMF-RendererVersion"] == RENDERER_VERSION
    assert parsed["X-RSMF-Generator"] == f"edisc-renderers/{RENDERER_VERSION}"
    assert parsed["X-RSMF-CollectionId"] == str(JOB.job_id)
    assert parsed.defects == []
    assert all(not p.defects for p in parsed.walk())


def test_render_job_refuses_a_message_given_twice() -> None:
    from edisc_renderers.rsmf import render_job

    m = msg("2026-01-05T09:00:00Z")
    with pytest.raises(RenderInputError, match="twice"):
        render_job(JOB, [conv()], [m, m], {}, {}, RenderOptions())


def test_unknown_conversation_type_is_omitted_and_said_so() -> None:
    [(_, parsed)] = render(slice_input(DAY, [msg("2026-01-05T09:00:00Z")], conversation=conv(None)))
    (c,) = parsed.manifest["conversations"]
    assert "type" not in c
    cust = {x["name"]: x["value"] for x in c["custom"]}
    assert cust["edisc.conversation_metadata"] == "not_collected"
    assert "slack.conversation_type" not in cust and "slack.kind" not in cust


async def test_async_and_sync_streams_give_the_same_bytes() -> None:
    from collections.abc import AsyncIterator

    data = b"x" * 5000
    m = msg("2026-01-05T09:00:00Z", files=("F1",))
    [f] = render_slice(
        slice_input(DAY, [m], files={"F1": attachment("F1", "a.bin", data)}), RenderOptions()
    )

    async def aopen(_: FileAttachment) -> AsyncIterator[bytes]:
        for i in range(0, len(data), 777):
            yield data[i : i + 777]

    streamed = b"".join([c async for c in f.astream(aopen)])
    # the synchronous driver runs its own loop, so an async caller uses it from a thread
    synced = await asyncio.to_thread(lambda: b"".join(f.stream(opener_for({"F1": data}))))
    assert streamed == synced


async def test_attachments_stream_through_with_bounded_memory() -> None:
    """A 64 MiB attachment goes through the zip and base64 without ever being held whole."""
    import hashlib
    import tracemalloc
    from collections.abc import AsyncIterator

    size, chunk = 64 << 20, b"\xab" * (1 << 20)
    digest = hashlib.sha256()
    for _ in range(size // len(chunk)):
        digest.update(chunk)
    big = FileAttachment("F1", "big.bin", size, digest.hexdigest(), ref("T0TEST/file/F1"), "F1")
    [f] = render_slice(
        slice_input(DAY, [msg("2026-01-05T09:00:00Z", files=("F1",))], files={"F1": big}),
        RenderOptions(),
    )

    async def aopen(_: FileAttachment) -> AsyncIterator[bytes]:
        for _ in range(size // len(chunk)):
            yield chunk

    tracemalloc.start()
    total = 0
    try:
        async for out in f.astream(aopen):
            total += len(out)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert total > size * 4 // 3  # base64 of the whole zip went through
    assert peak < 8 << 20, f"peak {peak / 2**20:.1f} MiB for a 64 MiB attachment"


def test_several_custodians_are_all_listed_and_names_stay_searchable() -> None:
    from edisc_core.time import parse_utc

    identities = {
        "U1": (
            Identity("U1", None, display_name="Alice", real_name="Alice Smith"),
            Identity("U1", parse_utc("2026-01-04T00:00:00Z"), display_name="Alice Jones"),
        ),
        "U2": (Identity("U2", display_name="Bob"),),
    }
    c = conv(
        custodians=("U1", "U2"), archived=True, topic="Ship it", purpose="Releases",
        known_names=("general", "general-old"),
    )  # fmt: skip
    [(_, parsed)] = render(
        slice_input(DAY, [msg("2026-01-05T09:00:00Z")], conversation=c, identities=identities)
    )
    (cv,) = parsed.manifest["conversations"]
    assert "custodian" not in cv  # several: the RSMF field stays empty, custom lists them all
    pairs = [(x["name"], x["value"]) for x in cv["custom"]]
    assert [v for n, v in pairs if n == "edisc.custodian"] == ["U1", "U2"]
    assert [v for n, v in pairs if n == "edisc.known_name"] == ["general", "general-old"]
    assert ("slack.is_archived", "true") in pairs and ("slack.topic", "Ship it") in pairs
    assert parsed.headers["X-RSMF-Custodian"] == "Alice Jones, Bob"
    people = {p["id"]: p for p in parsed.manifest["participants"]}
    alice = [(x["name"], x["value"]) for x in people["U1"]["custom"]]
    assert people["U1"]["display"] == "Alice Jones"  # in force at the slice
    assert [v for n, v in alice if n == "edisc.known_name"] == [
        "Alice",
        "Alice Jones",
        "Alice Smith",
    ]
    assert ("slack.user_id", "U1") in alice


def test_custodians_must_be_sorted_and_unique() -> None:
    with pytest.raises(RenderInputError):
        conv(custodians=("U2", "U1"))


def test_reactions_recorded_before_a_deletion_are_history_only() -> None:
    """A deleted message keeps the reactions last observed, as history (like its earlier text in
    `edits`): no RSMF `reactions` (which would say they exist now), custom entries instead, and the
    people who reacted stay participants."""
    reactions = (("eyes", ("U3",)), ("thumbsup", ("U2", "U3")))
    live = msg("2026-01-05T09:00:00Z", reactions=reactions)
    gone = msg(
        "2026-01-05T10:00:00Z", author="U1", deleted=True, edits=(("before", None),),
        reactions=(*reactions, ("tada", ("U9",))), deleted_ts=ts_at("2026-01-05T11:00:00Z"),
    )  # fmt: skip
    [(_, parsed)] = render(slice_input(DAY, [live, gone]))
    events = _events(parsed)
    alive, deleted = events[live.ts], events[gone.ts]
    assert [r["value"] for r in alive["reactions"]] == ["eyes", "thumbsup"]
    assert "reactions" not in deleted and deleted["deleted"] is True
    assert custom(deleted)["edisc.reactions_before_deletion"] == [
        "eyes (1): U3",
        "tada (1): U9",
        "thumbsup (2): U2,U3",
    ]
    assert "edisc.reactions_before_deletion" not in custom(alive)
    assert "U9" in {p["id"] for p in parsed.manifest["participants"]}  # only reacted, then deleted
