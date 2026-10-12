"""The HTML preview renderer (ADR 0018 §1.2, §3.2, §4.7, §10, §15), pure: a page parses under
html5lib STRICT with only allowed elements, attributes and URLs (no handler, no remote URL, no image,
no script; the one `<style>` is the CSP's), every user string revealed, the day split into pages that
hold every message once, banners for incomplete and empty days, byte-identical output."""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import html5lib
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from edisc_core.time import format_utc
from edisc_renderers.preview import (
    CSP_HEADER,
    CSP_META,
    STYLE,
    DayUnit,
    PreviewInput,
    PreviewJob,
    PreviewPageError,
    plan_pages,
    render_preview,
)
from edisc_renderers.rsmf import (
    ConversationInfo,
    FileAttachment,
    FileUnavailable,
    Identity,
    ItemRef,
    JobInfo,
    Message,
    MessageState,
    Reactions,
    SliceInput,
)

DAY = date(2026, 1, 5)
HANDLE = "0192f3a4-7c1e-7d2a-9b5e-3f0c2d1e4a5b"
ALLOWED_TAGS = {"html", "head", "meta", "title", "style", "body", "h1", "p", "table", "tbody", "tr",
                "th", "td", "ol", "ul", "li", "div", "span", "a", "time", "bdi"}  # fmt: skip
ALLOWED_ATTRS = {"class", "id", "href", "datetime", "rel", "charset", "content", "http-equiv",
                 "name", "lang"}  # fmt: skip
HREF = re.compile(r"^(/v1/evidence/[0-9a-f-]{36}/content\?purpose=preview|#m-[0-9-]+)$")
HOSTILE = ("‮evil.exe‬ <img src=https://127.0.0.1:9/x.png> <link href=//127.0.0.1:9/s.css>"
           " url(https://127.0.0.1:9/y) @import 'https://127.0.0.1:9/z'; https://127.0.0.1:9/p"
           " <script>alert(1)</script> zero​width nul\x00 " + chr(0xD83D))  # fmt: skip


def _ref(sid: str, version: int = 1) -> ItemRef:
    h = hashlib.sha256(f"{sid}/{version}".encode()).hexdigest()
    return ItemRef(sid, version, h, hashlib.sha256(h.encode()).hexdigest())


def _ts(minute: int, day: date = DAY) -> str:
    at = datetime(day.year, day.month, day.day, 9, tzinfo=UTC) + timedelta(minutes=minute)
    return f"{int(at.timestamp())}.000100"


def _msg(ts: str, text: str = "hello", *, root: str | None = None, versions: int = 1,
         deleted: bool = False, files: tuple[str, ...] = (), reactions: bool = False,
         subtype: str | None = None, in_scope: bool = True) -> Message:  # fmt: skip
    sid = f"T1/C1/{ts}"
    states = [MessageState(_ref(sid, v), "U1", f"{text} v{v}" if v < versions else text, subtype,
                           root, False, files) for v in range(1, versions + 1)]  # fmt: skip
    if deleted:
        states.append(
            MessageState(_ref(sid, versions + 1), "U1", "", "message_deleted", root, True)
        )
    sent = datetime.fromtimestamp(int(ts.split(".")[0]), UTC) + timedelta(microseconds=100)
    rx = Reactions(_ref(f"{sid}#reactions"), (("tada", ("U1", "U2")),)) if reactions else None
    return Message("C1", ts, sent, in_scope, tuple(states), rx)


def _inp(messages: list[Message], roots: dict[str, Message] | None = None, *,
         missing: frozenset[str] = frozenset(), units: tuple[DayUnit, ...] | None = None,
         zone: str = "UTC", day: date = DAY, basis: str = "source", name: str = "general",
         ) -> PreviewInput:  # fmt: skip
    files = {
        "F1": FileAttachment("F1", "report <b>.pdf", 10, "ab" * 32, _ref("T1/file/F1"), HANDLE),
        "F2": FileUnavailable("F2", "gone.txt", "expired_url", _ref("T1/file/F2#availability")),
        "F3": FileAttachment("F3", "odd", 1, "cd" * 32, _ref("T1/file/F3"), "not-a-uuid"),
    }
    sl = SliceInput(
        job=JobInfo(uuid.UUID(HANDLE), "0.5.0", "0.3.0", basis),  # type: ignore[arg-type]
        conversation=ConversationInfo("C1", "public_channel", "T1", name=name),
        day=day, messages=tuple(messages), roots=roots or {}, missing_roots=missing,
        identities={"U1": (Identity("U1", display_name="Alice " + chr(0x202E) + "x"),)},
        files=files,
    )  # fmt: skip
    default = (DayUnit("C1/2026-01-05", "done", "matched", len(messages), len(messages), 0),)
    return PreviewInput(PreviewJob(HANDLE, "completed", True, basis), sl, zone,  # type: ignore[arg-type]
                        default if units is None else units)  # fmt: skip


def _strict(page: bytes) -> Any:
    """Parse under html5lib STRICT (any parse error raises) and check every element, attribute,
    URL and style against the allow-list."""
    parser = html5lib.HTMLParser(strict=True, namespaceHTMLElements=False)
    doc = parser.parse(page.decode("utf-8"))
    styles = []
    for node in doc.iter():
        assert isinstance(node.tag, str) and node.tag in ALLOWED_TAGS, node.tag
        for k, v in node.attrib.items():
            assert k in ALLOWED_ATTRS and not k.startswith("on"), (k, v)
            if k == "href":
                assert HREF.match(v), v
        if node.tag == "style":
            styles.append(node.text or "")
    assert styles == [STYLE]
    want = "sha256-" + base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
    assert f"style-src '{want}'" in CSP_HEADER and f"style-src '{want}'" in CSP_META
    return doc


def _text(doc: Any) -> str:
    return "".join(doc.itertext())


def test_a_page_is_safe_complete_and_deterministic() -> None:
    root = _msg(_ts(0, DAY - timedelta(days=1)), "root from yesterday")
    messages = [
        _msg(_ts(1), HOSTILE, files=("F1", "F2", "F3"), reactions=True),
        _msg(_ts(2), "edited", versions=3),
        _msg(_ts(3), "gone", deleted=True, reactions=True),
        _msg(_ts(4), "a reply", root=root.ts),
        _msg(_ts(5), "orphan reply", root=_ts(0, DAY - timedelta(days=9))),
        _msg(_ts(6), "", subtype="channel_join"),
    ]
    inp = _inp(messages, {root.ts: root}, missing=frozenset({_ts(0, DAY - timedelta(days=9))}))
    page = render_preview(inp, 1)
    assert render_preview(inp, 1).html == page.html  # byte-identical
    doc = _strict(page.html)
    text = _text(doc)
    raw = page.html.decode()
    assert (page.page, page.pages, page.events, page.context_events) == (1, 1, 6, 1)
    # remote URLs and markup in message text are text, never attributes or links
    assert "<img" not in raw and "<link" not in raw and "<script" not in raw
    assert "https://127.0.0.1:9/x.png" in text and "@import" in text
    assert raw.count("<a ") == raw.count('href="/v1/evidence/') + raw.count('href="#m-')
    assert f'href="/v1/evidence/{HANDLE}/content?purpose=preview"' in raw
    assert "not-a-uuid" not in raw  # a handle that is not an evidence id is never linked
    # revealed: bidi replaced, zero-width kept with its marker, NUL and the lone surrogate replaced
    for cp in ("U+202E", "U+202C", "U+200B", "U+0000", "U+D83D"):
        assert f'<span class="cp">{cp}</span>' in raw, cp
    assert "‮" not in raw and "\x00" not in raw
    # what a reader must see
    assert "Deleted (tombstone)" in text and "Text before deletion: gone" in text
    assert "Reactions before deletion (history)" in text
    assert "Earlier versions:" in text and "version 1: edited v1" in text
    assert "File not collected: gone.txt (F2), reason: expired_url" in text
    assert "context: thread root, in the collection" in text
    assert "root not collected" in text and "joined" in text
    assert f"slice source hash {page.source_hash}" in text


def test_pages_hold_every_message_once_at_n_and_n_plus_one() -> None:
    messages = [_msg(_ts(i)) for i in range(5)]
    assert [len(p) for p in plan_pages(_inp(messages).slice, cap=5)] == [5]
    six = [*messages, _msg(_ts(5))]
    pages = plan_pages(_inp(six).slice, cap=5)
    assert [len(p) for p in pages] == [5, 1]
    assert sorted(m.ts for p in pages for m in p) == sorted(m.ts for m in six)
    with pytest.raises(PreviewPageError):
        render_preview(_inp(messages), 2)


def test_an_empty_day_and_an_incomplete_day_say_so() -> None:
    empty = render_preview(
        _inp([], units=(DayUnit("C1/2026-01-05", "done", "matched", 0, 0, 0),)), 1
    )
    assert "0 messages (unit status: matched, expected 0)" in _text(_strict(empty.html))
    gap = (
        DayUnit("C1/2026-01-05", "done", "gap", 120, 117, 0, None, ("thread_ts_lone_surrogate",)),
    )
    page = render_preview(_inp([_msg(_ts(1))], units=gap), 1)
    text = _text(_strict(page.html))
    assert "NOT COMPLETE for this day" in text and "Expected 120, collected 117" in text
    assert "thread_ts_lone_surrogate" in text
    archive = render_preview(_inp([_msg(_ts(1))], basis="archive"), 1)
    assert "COMPLETE RELATIVE TO THE PROVIDED EXPORT ONLY" in _text(_strict(archive.html))


def test_a_day_in_a_time_zone_is_bounded_by_its_local_midnights() -> None:
    zone_day = date(2026, 1, 5)
    # Asia/Kathmandu (+05:45): the local day starts at 18:15 UTC the day before
    inside = _msg(f"{int(datetime(2026, 1, 4, 18, 15, tzinfo=UTC).timestamp())}.000100")
    page = render_preview(_inp([inside], zone="Asia/Kathmandu", day=zone_day), 1)
    text = _text(_strict(page.html))
    assert format_utc(datetime(2026, 1, 4, 18, 15, tzinfo=UTC)) in text
    assert page.identity["tzdata"] and page.identity["time_zone"] == "Asia/Kathmandu"


@settings(max_examples=60, deadline=None)
@given(st.text(alphabet=st.characters(codec=None, exclude_categories=()), max_size=40))
def test_any_user_string_parses_strictly_and_stays_text(value: str) -> None:
    page = render_preview(_inp([_msg(_ts(1), value)], name=value), 1)
    _strict(page.html)
