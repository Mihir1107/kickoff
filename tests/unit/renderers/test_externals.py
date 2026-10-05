"""Oversized attachments as natives outside the zip (ADR 0015 §11, §20), in the pure renderer:
which attachments leave (policy threshold, then the exact ZIP64 rule), the pinned placeholder bytes,
the entry-count split into parts, and that a native's bytes are never read by the renderer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import date

import pytest

import edisc_renderers.rsmf.render as render_module
from edisc_custody.zipwriter import MAX16, MAX32
from edisc_renderers.rsmf import (
    FileAttachment,
    Reconciler,
    ReconciliationError,
    RenderedFile,
    RenderInputError,
    RenderOptions,
    render_slice,
    subject_digest,
)
from edisc_renderers.rsmf.model import EXTERNAL_OVER_MAX, EXTERNAL_OVER_MIN
from edisc_renderers.rsmf.zipstream import sizer
from tests.unit.renderers.builders import (
    attachment,
    msg,
    opener_for,
    ref,
    slice_input,
)
from tests.unit.renderers.emlcheck import check_eml, custom

DAY = date(2026, 1, 5)
MIB = 1 << 20
WIDE_OPEN = RenderOptions(external_over_bytes=EXTERNAL_OVER_MAX)  # only the ZIP64 rule applies


def sized(fid: str, size: int, name: str = "big.bin") -> FileAttachment:
    """A file of `size` bytes that is never read (its bytes do not exist)."""
    return FileAttachment(
        fid, name, size, hashlib.sha256(fid.encode()).hexdigest(), ref(f"T0TEST/file/{fid}"), fid
    )


def one_file(f: FileAttachment, options: RenderOptions) -> RenderedFile:
    m = msg("2026-01-05T09:00:00Z", files=(f.file_id,))
    [out] = render_slice(slice_input(DAY, [m], files={f.file_id: f}), options)
    return out


def never(f: FileAttachment) -> Iterator[bytes]:
    raise AssertionError(f"the renderer read native {f.file_id}")


def externals_of(f: RenderedFile) -> dict[str, str]:
    """file id -> reason, read from the placeholder entries planned for the zip."""
    out = {}
    for e in f.entries:
        if e.name.endswith("_EXTERNAL.txt"):
            assert e.data is not None and e.file is None
            fields = dict(line.split(": ", 1) for line in e.data.decode().splitlines())
            out[e.name.removesuffix("_EXTERNAL.txt")] = fields["reason"]
    assert sorted(out) == [a.file_id for a in f.externals]
    return out


# ------------------------------------------------------------------ the policy threshold
def test_exactly_at_the_threshold_stays_one_byte_over_leaves() -> None:
    options = RenderOptions(external_over_bytes=2 * MIB)
    at = one_file(sized("F1", 2 * MIB), options)
    assert at.external_count == 0 and at.externals == ()
    over = one_file(sized("F1", 2 * MIB + 1), options)
    assert over.external_count == 1 and [a.file_id for a in over.externals] == ["F1"]
    assert externals_of(over) == {"F1": "over_external_threshold"}


def test_the_threshold_is_bounded_and_in_the_identity() -> None:
    for bad in (EXTERNAL_OVER_MIN - 1, EXTERNAL_OVER_MAX + 1, 0, True, 1.5e6):
        with pytest.raises(RenderInputError):
            RenderOptions(external_over_bytes=bad)  # type: ignore[arg-type]
    assert RenderOptions(external_over_bytes=EXTERNAL_OVER_MIN).external_over_bytes == MIB
    assert RenderOptions().as_payload()["external_over_bytes"] == 1 << 30
    assert RenderOptions().as_payload() != RenderOptions(external_over_bytes=2 * MIB).as_payload()


# ------------------------------------------------------------------ the structural rule (exact)
def _end_of_directory(f: RenderedFile) -> int:
    s = sizer(f.entries)
    return s.offset + s.cd_size


def test_the_zip64_boundary_is_exact() -> None:
    """The largest file that keeps the part's zip free of ZIP64 stays; one byte more leaves. Sizes
    keep ten digits, so the manifest has the same length on both sides of the computation."""
    base = 1_000_000_000
    fits = base + (MAX32 - 1 - _end_of_directory(one_file(sized("F1", base), WIDE_OPEN)))
    stays = one_file(sized("F1", fits), WIDE_OPEN)
    assert stays.external_count == 0
    assert _end_of_directory(stays) == MAX32 - 1 and not sizer(stays.entries).needs_zip64()
    leaves = one_file(sized("F1", fits + 1), WIDE_OPEN)
    assert [a.file_id for a in leaves.externals] == ["F1"]
    assert externals_of(leaves) == {"F1": "exceeds_rsmf_zip_limit"}


def test_a_single_attachment_over_4_gib_always_leaves() -> None:
    assert externals_of(one_file(sized("F1", 5 << 30), WIDE_OPEN)) == {
        "F1": "over_external_threshold"
    }
    assert externals_of(one_file(sized("F1", MAX32), WIDE_OPEN)) == {"F1": "exceeds_rsmf_zip_limit"}


def test_largest_first_until_no_zip64_is_needed() -> None:
    files = {"F1": sized("F1", 1 << 30), "F2": sized("F2", 3 << 30), "F3": sized("F3", 1 << 29)}
    m = msg("2026-01-05T09:00:00Z", files=("F1", "F2", "F3"))
    [f] = render_slice(slice_input(DAY, [m], files=files), WIDE_OPEN)
    assert [a.file_id for a in f.externals] == ["F2"]  # 4.5 GiB in all: the largest goes, enough


def test_ties_are_broken_by_file_id() -> None:
    files = {fid: sized(fid, 3 << 30) for fid in ("F9", "F2", "F5")}
    m = msg("2026-01-05T09:00:00Z", files=("F9", "F2", "F5"))
    [f] = render_slice(slice_input(DAY, [m], files=files), WIDE_OPEN)
    assert [a.file_id for a in f.externals] == ["F2", "F5"]  # 3 GiB may stay: F9, the last by id


def test_policy_externals_come_before_the_structural_rule() -> None:
    options = RenderOptions(external_over_bytes=2 << 30)
    files = {
        "F1": sized("F1", (2 << 30) + 1),
        "F2": sized("F2", 2 << 30),
        "F3": sized("F3", 1 << 30),
    }
    m = msg("2026-01-05T09:00:00Z", files=("F1", "F2", "F3"))
    [f] = render_slice(slice_input(DAY, [m], files=files), options)
    assert externals_of(f) == {"F1": "over_external_threshold"}  # F2 + F3 = 3 GiB fit


# ------------------------------------------------------------------ the RSMF side
def test_placeholder_bytes_are_pinned() -> None:
    name = "Cafe\u0301 report\n\u2028x.pdf"  # decomposed e-acute, a newline, a line separator
    f = one_file(sized("F1", 2 * MIB, name=name), RenderOptions(external_over_bytes=MIB))
    parsed = check_eml(b"".join(f.stream(never)))
    sha = hashlib.sha256(b"F1").hexdigest()
    expected = (
        "name: Caf\u00e9 report\\u000a\\u2028x.pdf\n"
        f"size: {2 * MIB}\n"
        f"sha256: {sha}\n"
        "reason: over_external_threshold\n"
        f"native: natives/{sha}\n"
    ).encode()
    assert parsed.zip.read("F1_EXTERNAL.txt") == expected
    assert not expected.startswith(b"\xef\xbb\xbf") and b"\r" not in expected
    [event] = parsed.manifest["events"]
    assert event["attachments"] == [
        {"id": "F1_EXTERNAL.txt", "display": name, "size": len(expected)}
    ]
    assert custom(event)["edisc.file_external"] == [f"F1: sha256:{sha}"]
    assert f.record()["external_count"] == 1
    assert "1 attachment(s) are delivered next to this file as natives" in parsed.text


def test_the_renderer_never_reads_a_native_and_streams_the_rest() -> None:
    small = attachment("F2", "small.txt", b"inline bytes")
    files = {"F1": sized("F1", 2 * MIB), "F2": small}
    m = msg("2026-01-05T09:00:00Z", files=("F1", "F2"))
    [f] = render_slice(slice_input(DAY, [m], files=files), RenderOptions(external_over_bytes=MIB))
    calls: list[str] = []

    def opener(file: FileAttachment) -> Iterator[bytes]:
        calls.append(file.file_id)
        if file.file_id == "F1":
            raise AssertionError("native read")
        yield b"inline bytes"

    data = b"".join(f.stream(opener))
    assert calls == ["F2"]
    parsed = check_eml(data)
    assert parsed.zip.read("F2_small.txt") == b"inline bytes"
    assert data == b"".join(f.stream(opener))  # byte identity


def test_the_same_file_in_two_events_is_one_external() -> None:
    files = {"F1": sized("F1", 2 * MIB)}
    ms = [msg("2026-01-05T09:00:00Z", files=("F1",)), msg("2026-01-05T10:00:00Z", files=("F1",))]
    [f] = render_slice(slice_input(DAY, ms, files=files), RenderOptions(external_over_bytes=MIB))
    assert f.external_count == 1 and f.attachment_count == 1
    parsed = check_eml(b"".join(f.stream(never)))
    assert [custom(e)["edisc.file_external"] for e in parsed.manifest["events"]] == [
        [f"F1: sha256:{files['F1'].sha256}"]
    ] * 2


# ------------------------------------------------------------------ the entry count splits parts
@pytest.fixture
def entry_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    """A tiny per-test entry limit: 6 entries per zip, so 5 attachments or placeholders per part."""
    monkeypatch.setattr(render_module, "MAX_PART_ENTRIES", 6)
    return 5


def _files(*fids: str) -> dict[str, FileAttachment]:
    return {fid: attachment(fid, f"{fid}.txt", fid.encode()) for fid in fids}


def test_exactly_at_the_entry_limit_stays_one_part(entry_limit: int) -> None:
    ms = [
        msg("2026-01-05T09:00:00Z", files=("A1", "A2", "A3")),
        msg("2026-01-05T10:00:00Z", files=("B1", "B2")),
    ]
    parts = render_slice(
        slice_input(DAY, ms, files=_files("A1", "A2", "A3", "B1", "B2")), RenderOptions()
    )
    assert len(parts) == 1 and parts[0].attachment_count == entry_limit


def test_one_entry_over_starts_a_new_part(entry_limit: int) -> None:
    ms = [
        msg("2026-01-05T09:00:00Z", files=("A1", "A2", "A3")),
        msg("2026-01-05T10:00:00Z", files=("B1", "B2", "B3")),
        msg("2026-01-05T11:00:00Z", text="no files"),
    ]
    files = _files("A1", "A2", "A3", "B1", "B2", "B3")
    parts = render_slice(slice_input(DAY, ms, files=files), RenderOptions())
    assert [(f.part, f.parts, f.event_count, f.attachment_count) for f in parts] == [
        (1, 2, 1, 3),
        (2, 2, 2, 3),
    ]
    blobs = {fid: fid.encode() for fid in files}
    for f in parts:
        check_eml(b"".join(f.stream(opener_for(blobs))))
    assert len({f.name for f in parts}) == 2


def test_a_context_root_counts_in_the_part_that_needs_it(entry_limit: int) -> None:
    root = msg("2026-01-04T09:00:00Z", files=("R1", "R2"))  # the day before: context only
    reply = msg("2026-01-05T09:00:00Z", root=root.ts, files=("C1", "C2"))
    other = msg("2026-01-05T10:00:00Z", root=root.ts, files=("D1", "D2"))
    files = _files("R1", "R2", "C1", "C2", "D1", "D2")
    inp = slice_input(DAY, [reply, other], roots={root.ts: root}, files=files)
    parts = render_slice(inp, RenderOptions())
    # root (2) + reply (2) = 4; the next reply (2) would make 6: a new part, with the root again
    assert [(f.event_count, f.context_event_count, f.attachment_count) for f in parts] == [
        (2, 1, 4),
        (2, 1, 4),
    ]
    rec = Reconciler(RenderOptions())
    rec.add_slice(inp, parts)
    rec.finish(2, subject_digest([reply.subject, other.subject]))


def test_one_event_that_cannot_fit_raises(entry_limit: int) -> None:
    m = msg("2026-01-05T09:00:00Z", files=("A1", "A2", "A3", "A4", "A5", "A6"))
    with pytest.raises(RenderInputError, match="zip entries"):
        render_slice(
            slice_input(DAY, [m], files=_files("A1", "A2", "A3", "A4", "A5", "A6")), RenderOptions()
        )


def test_the_default_entry_limit_is_the_largest_without_zip64() -> None:
    assert render_module.MAX_PART_ENTRIES == MAX16 - 1 == 65_534
    assert render_module.MAX_PART_ENTRIES - render_module.FIXED_ENTRIES == 65_535 - 2  # §20.10


# ------------------------------------------------------------------ reconciliation of natives
def _reconciled(options: RenderOptions) -> tuple[Reconciler, FileAttachment]:
    big = sized("F1", 2 * MIB)
    m = msg("2026-01-05T09:00:00Z", files=("F1",))
    inp = slice_input(DAY, [m], files={"F1": big})
    rec = Reconciler(options)
    rec.add_slice(inp, render_slice(inp, options))
    return rec, big


def test_the_reconciler_checks_the_written_natives() -> None:
    options = RenderOptions(external_over_bytes=MIB)
    rec, big = _reconciled(options)
    assert rec.natives == {big.sha256: big.size}
    rec.check_natives([(big.sha256, big.size)])
    with pytest.raises(ReconciliationError, match="not written"):
        rec.check_natives([])
    with pytest.raises(ReconciliationError, match="not written"):  # a native with another hash
        rec.check_natives([("0" * 64, big.size)])
    with pytest.raises(ReconciliationError, match="another size"):
        rec.check_natives([(big.sha256, big.size + 1)])
    with pytest.raises(ReconciliationError, match="not referenced"):
        rec.check_natives([(big.sha256, big.size), ("1" * 64, 5)])
    with pytest.raises(ReconciliationError, match="twice"):
        rec.check_natives([(big.sha256, big.size), (big.sha256, big.size)])
    m = rec.finish(1, subject_digest([msg("2026-01-05T09:00:00Z").subject]))
    assert (m.attachments, m.external_attachments) == (1, 1)


def test_an_external_reference_that_was_not_planned_fails() -> None:
    import dataclasses

    from edisc_core.canonical import canonical_json

    options = RenderOptions(external_over_bytes=MIB)
    big = sized("F1", 2 * MIB)
    m = msg("2026-01-05T09:00:00Z", files=("F1",))
    inp = slice_input(DAY, [m], files={"F1": big})
    [f] = render_slice(inp, options)
    manifest = json.loads(f.manifest)
    for pair in manifest["events"][0]["custom"]:
        if pair["name"] == "edisc.file_external":
            pair["value"] = f"F1: sha256:{'0' * 64}"
    tampered = dataclasses.replace(f, manifest=canonical_json(manifest))
    with pytest.raises(ReconciliationError, match="not planned"):
        Reconciler(options).add_slice(inp, [tampered])
    dropped = dataclasses.replace(f, externals=())
    with pytest.raises(ReconciliationError, match="not planned"):
        Reconciler(options).add_slice(inp, [dropped])
    uncounted = dataclasses.replace(f, external_count=0)
    with pytest.raises(ReconciliationError, match="differ from the plan"):
        Reconciler(options).add_slice(inp, [uncounted])
