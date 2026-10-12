"""The HTML preview of one conversation-day page (ADR 0018 §1.2, §3.2, §4.7, §10).

PURE (no DB, S3 or clock; the purity test covers it) and byte-identical for the same input and
``PREVIEW_RENDERER_VERSION`` (plus Unicode, and tzdata for a day in a time zone): no request id, no
generation time, no host name. A preview is not a production: nothing promises the same bytes on a
later runtime, and nothing is stored.

**Input.** A `SliceInput` holding EVERY in-scope message of the day (the loader's
`RenderLoader.day_slice`), with their thread roots (any day, any scope) or the roots declared missing,
identities and file outcomes. ``plan_pages`` splits the day's messages in event order into pages of at
most ``PREVIEW_PAGE_EVENTS`` events, counting the thread roots each page shows as context (the RSMF
part split, `split_parts`); every message is on exactly one page (checked: the preview's
reconciliation). ``render_preview`` builds one page.

**Safety (§3.2, §10).** No template engine: an element builder whose only text path is `escape`.
Every user string (names, message text, file names, reasons, ids) goes through ``user``: bidi
controls and code points invalid in HTML REPLACED by a marker element, other Cf/Cc/Zl/Zp kept with
the marker after them, escaped, wrapped in `<bdi>` (the report's rules, without its glyph coverage,
which exists for the PDF). URLs in message text are text, never links. The ONLY links are
attachments, to ``/v1/evidence/<uuid>/content?purpose=preview`` (the pinned file evidence id), and
fragment links to events of the same page. No image, script, form, iframe or external resource; one
constant inline `<style>` whose SHA-256 is in the CSP (header and `<meta>`).

**What a reader sees (§4.7).** The job's status verbatim and whether it is clean; the archive caveat
when the job is archive-relative; every unit covering the day (UTC unit days) with its status,
expected, collected, file gaps, access-lost and gap reasons, and a banner for a day that is not
complete ("Expected 120, collected 117"); "0 messages (unit status: ...)" for an empty day; edits with
the earlier texts, tombstones with the text before deletion, reactions (a deleted message's only as
history), unavailable files with their reason, thread context marked as such; the page identity
(slice source hash, preview renderer version).
"""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from edisc_core.schemas import ARCHIVE_CAVEAT
from edisc_core.time import format_utc, from_epoch
from edisc_custody.merkle import batch_root
from edisc_renderers.report.html import BIDI_CONTROLS, escape, invalid_in_html, marker
from edisc_renderers.rsmf.model import (
    FileAttachment,
    FileOutcome,
    FileUnavailable,
    Identity,
    Message,
    RenderInputError,
    SliceInput,
)
from edisc_renderers.rsmf.runtime import TZDATA_VERSION, load_zone
from edisc_renderers.rsmf.slicing import event_order, slice_bounds, split_parts

PREVIEW_RENDERER_VERSION = "1.0.0"
"""Byte identity of a page is promised for the same input AND this version (+ Unicode, + tzdata for a
day in a time zone). Any change to the bytes needs a bump."""
PREVIEW_PAGE_EVENTS = 1_000  # events per page, thread context included (§1.2)

_REVEAL_CATEGORIES = frozenset({"Cf", "Cc", "Zl", "Zp"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_JOIN_LEAVE = {"channel_join": "joined", "group_join": "joined", "channel_leave": "left",
               "group_leave": "left"}  # fmt: skip
NOT_COMPLETE = ("gap", "unverifiable", "access_lost", "failed", "surplus")


class PreviewPageError(ValueError):
    """The page asked for does not exist (the day has fewer pages)."""


# ------------------------------------------------------------------ input
@dataclass(frozen=True)
class DayUnit:
    """A unit whose UTC day overlaps the previewed day, as recorded for the job."""

    unit_key: str
    status: str
    recon_status: str
    expected: int | None
    collected: int | None
    file_gaps: int | None
    access_lost_reason: str | None = None
    gap_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewJob:
    job_id: str
    status: str  # verbatim
    clean: bool  # the job status alone is clean (the report decides custody-level cleanliness)
    basis: Literal["source", "archive"]


@dataclass(frozen=True)
class PreviewInput:
    job: PreviewJob
    slice: SliceInput  # every in-scope message of the day, its roots, identities and files
    time_zone: str
    units: tuple[DayUnit, ...] = ()


@dataclass(frozen=True)
class PreviewPage:
    html: bytes
    page: int
    pages: int
    events: int  # primaries on this page
    context_events: int
    day_messages: int  # in-scope messages of the whole day (every page)
    source_hash: str  # Merkle root over the page's items (as an RSMF part's)
    identity: dict[str, str] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.html).hexdigest()


# ------------------------------------------------------------------ sanitiser and builder
def replaced(cp: int) -> bool:
    """Dropped and shown only as the marker: bidi controls and code points invalid in HTML."""
    return cp in BIDI_CONTROLS or invalid_in_html(cp)


def reveal(text: str) -> str:
    """``text`` as safe HTML: escaped runs and a marker for every invisible, reordering or
    HTML-invalid character (replacing it, or after it when kept). The report's rules (§3.2)."""
    out: list[str] = []
    run: list[str] = []
    for ch in text:
        cp = ord(ch)
        if replaced(cp):
            out.append(escape("".join(run)))
            run.clear()
            out.append(marker(cp))
        elif unicodedata.category(ch) in _REVEAL_CATEGORIES:
            run.append(ch)
            out.append(escape("".join(run)))
            run.clear()
            out.append(marker(cp))
        else:
            run.append(ch)
    out.append(escape("".join(run)))
    return "".join(out)


def user(value: Any) -> str:
    return f"<bdi>{reveal('' if value is None else str(value))}</bdi>"


def _attrs(attrs: Mapping[str, str]) -> str:
    return "".join(f' {k}="{escape(v)}"' for k, v in sorted(attrs.items()))


def el(tag: str, inner: str = "", **attrs: str) -> str:
    return f"<{tag}{_attrs(attrs)}>{inner}</{tag}>"


def num(value: int | None) -> str:
    return "UNKNOWN (not recorded)" if value is None else f"{value:,}"


STYLE = (
    "html{font-family:system-ui,sans-serif;font-size:14px;color:#111;background:#fff;margin:0}"
    "body{max-width:60rem;margin:0 auto;padding:1rem}h1{font-size:1.3rem}"
    ".banner{border:2px solid #000;padding:.5rem .8rem;margin:.5rem 0;font-weight:700}"
    ".banner.notclean{background:#fff0f0;border-color:#a00}"
    ".banner.clean{background:#f0fff0;border-color:#0a0}"
    "table{border-collapse:collapse;width:100%;margin:.3rem 0;table-layout:fixed}"
    "th,td{border:1px solid #bbb;padding:2px 5px;text-align:left;vertical-align:top;"
    "overflow-wrap:anywhere}th{background:#f3f3f3}"
    "ol.events{list-style:none;padding:0}li.ev{border-top:1px solid #ddd;padding:.4rem 0}"
    "li.ev.ctx{background:#f6f6ff}.meta{color:#555;font-size:.85em}"
    ".tx{white-space:pre-wrap;overflow-wrap:anywhere;margin:.2rem 0}"
    ".tomb{color:#a00;font-weight:700}.hist{color:#555;font-size:.9em}"
    ".ctxmark{color:#335;font-style:italic}bdi{unicode-bidi:isolate}"
    ".cp{font-family:monospace;font-size:.78em;color:#fff;background:#5a0a8a;border-radius:2px;"
    "padding:0 2px;margin:0 1px;white-space:nowrap;unicode-bidi:isolate;direction:ltr}"
)
STYLE_CSP_HASH = (
    f"sha256-{base64.b64encode(hashlib.sha256(STYLE.encode('utf-8')).digest()).decode('ascii')}"
)
_DENY = (
    f"default-src 'none'; style-src '{STYLE_CSP_HASH}'; img-src 'none'; font-src 'none'; "
    "connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; "
    "form-action 'none'"
)
CSP_HEADER = f"{_DENY}; frame-ancestors 'self'; sandbox allow-top-navigation-by-user-activation"
# `frame-ancestors` and `sandbox` are honoured only in the header (a <meta> policy ignores them)
CSP_META = _DENY


# ------------------------------------------------------------------ planning
def _root_of(m: Message) -> str | None:
    root = m.current.thread_root
    return root if root and root != m.ts else None


def _lookup(inp: SliceInput) -> tuple[dict[str, Message], dict[str, Message]]:
    """The day's messages by ts (checked like an RSMF slice) and every message a reply may point to."""
    conv = inp.conversation
    by_ts: dict[str, Message] = {}
    for m in inp.messages:
        if not m.in_scope:
            raise RenderInputError(f"{m.subject}: out-of-scope items are only shown as context")
        if m.conversation_id != conv.id:
            raise RenderInputError(f"{m.subject} belongs to {m.conversation_id}, not {conv.id}")
        if from_epoch(m.ts) != m.sent_at:
            raise RenderInputError(f"{m.subject}: sent_at does not match ts {m.ts}")
        if m.ts in by_ts:
            raise RenderInputError(f"{m.subject} given twice")
        by_ts[m.ts] = m
    lookup = dict(by_ts)
    for ts, given in inp.roots.items():
        if given.ts != ts or _root_of(given) is not None or given.conversation_id != conv.id:
            raise RenderInputError(f"{given.subject} is not a thread root under key {ts}")
        lookup.setdefault(ts, given)
    for m in by_ts.values():
        root = _root_of(m)
        if root is not None and root not in lookup and root not in inp.missing_roots:
            raise RenderInputError(
                f"{m.subject}: root {root} neither provided nor declared missing"
            )
    return by_ts, lookup


def plan_pages(inp: SliceInput, cap: int = PREVIEW_PAGE_EVENTS) -> list[list[Message]]:
    """The day's messages in event order, split into pages of at most ``cap`` events (context roots
    counted). Every message on exactly one page, or this raises (the preview's reconciliation)."""
    by_ts, lookup = _lookup(inp)
    primaries = sorted(by_ts.values(), key=event_order)

    def context_root(m: Message) -> str | None:
        root = _root_of(m)
        return root if root is not None and root in lookup else None

    pages = split_parts(primaries, context_root, cap) if primaries else [[]]
    placed = [m.ts for p in pages for m in p]
    if sorted(placed) != sorted(by_ts) or len(placed) != len(set(placed)):
        raise RenderInputError(f"{inp.conversation.id}/{inp.day}: pages do not cover the day once")
    return pages


# ------------------------------------------------------------------ rendering
def _identity_at(snapshots: Sequence[Identity], end: datetime) -> Identity | None:
    chosen: Identity | None = None
    for s in sorted(
        snapshots, key=lambda s: (s.effective_from is not None, s.effective_from or end)
    ):
        if s.effective_from is None or s.effective_from < end:
            chosen = s
    return chosen


def _who(user_id: str, inp: SliceInput, end: datetime) -> str:
    ident = _identity_at(inp.identities.get(user_id, ()), end)
    name = None
    if ident is not None:
        name = next((n for n in (ident.display_name, ident.real_name) if n and n.strip()), None)
    return f"{user(name)} ({user(user_id)})" if name else user(user_id)


def _anchor(ts: str) -> str:
    return "m-" + "".join(c if c.isalnum() else "-" for c in ts)


def _when(m: Message, zone_name: str) -> str:
    utc = format_utc(m.sent_at)
    shown = escape(utc)
    if zone_name != "UTC":
        local = m.sent_at.astimezone(load_zone(zone_name)).isoformat()
        shown += f" ({escape(local)} {escape(zone_name)})"
    return el("time", shown, datetime=utc)


def _files(m: Message, files: Mapping[str, FileOutcome]) -> list[str]:
    out = []
    for fid in () if m.current.deleted else m.current.file_ids:
        f = files.get(fid)
        if f is None:
            raise RenderInputError(f"{m.subject}: no outcome for file {fid}")
        if isinstance(f, FileAttachment):
            label = f"{user(f.name or fid)} ({num(f.size)} bytes, SHA-256 {escape(f.sha256)})"
            if _UUID.match(f.handle):
                href = f"/v1/evidence/{f.handle}/content?purpose=preview"
                out.append(el("li", el("a", label, href=href, rel="noreferrer")))
            else:  # never a link to anything but pinned evidence
                out.append(el("li", label))
        elif isinstance(f, FileUnavailable):
            out.append(el("li", f"File not collected: {user(f.name or fid)} ({user(fid)}),"
                                f" reason: {user(f.reason)}"))  # fmt: skip
    return out


def _event(m: Message, inp: SliceInput, zone_name: str, end: datetime, *, context: str | None,
           on_page: set[str], lookup: Mapping[str, Message]) -> str:  # fmt: skip
    cur = m.current
    parts = [el("p", f"{_when(m, zone_name)} {_who(cur.author, inp, end)}"
                     + (f" {el('span', escape(context), **{'class': 'ctxmark'})}" if context else "")
                     + (f" {el('span', 'subtype: ' + user(cur.subtype), **{'class': 'meta'})}"
                        if cur.subtype else ""),
                **{"class": "meta"})]  # fmt: skip
    root = _root_of(m)
    if root is not None:
        if root in on_page:
            parts.append(el("p", "Reply in thread " + el("a", escape(root), href="#" + _anchor(root)),
                            **{"class": "meta"}))  # fmt: skip
        elif root in lookup:
            parts.append(el("p", f"Reply in thread {escape(root)} (root on another page)",
                            **{"class": "meta"}))  # fmt: skip
        else:
            parts.append(el("p", f"Reply in thread {escape(root)}: root not collected",
                            **{"class": "meta"}))  # fmt: skip
    if cur.subtype in _JOIN_LEAVE:
        parts.append(el("p", f"{_who(cur.author, inp, end)} {_JOIN_LEAVE[cur.subtype]}"))
    if cur.deleted:
        before = next((s.text for s in reversed(m.states[:-1]) if not s.deleted and s.text), None)
        parts.append(el("p", "Deleted (tombstone)", **{"class": "tomb"}))
        if before is not None:
            parts.append(el("div", "Text before deletion: " + user(before), **{"class": "hist"}))
    elif cur.text:
        parts.append(el("div", user(cur.text), **{"class": "tx"}))
    if len(m.states) > 1:  # every earlier version collected, in order (history, never current)
        rows = "".join(
            el("li", f"version {v.item.version}: " + ("(deleted)" if v.deleted else user(v.text)))
            for v in m.states[:-1]
        )
        parts.append(el("div", "Earlier versions:" + el("ul", rows), **{"class": "hist"}))
    if m.reactions is not None and m.reactions.reactions:
        listed = "; ".join(
            f"{user(name)} ({len(users)}): {', '.join(user(u) for u in sorted(users))}"
            for name, users in sorted(m.reactions.reactions)
        )
        label = "Reactions before deletion (history): " if cur.deleted else "Reactions: "
        parts.append(el("p", label + listed, **{"class": "hist" if cur.deleted else "meta"}))
    files = _files(m, inp.files)
    if files:
        parts.append(el("ul", "".join(files)))
    cls = "ev ctx" if context else "ev"
    return el("li", "".join(parts), id=_anchor(m.ts), **{"class": cls})


def _leaves(messages: Iterable[Message], files: Mapping[str, FileOutcome]) -> str:
    leaves: dict[str, str] = {}
    for m in messages:
        for s in m.states:
            leaves.setdefault(s.item.idempotency_key, s.item.content_hash)
        if m.reactions is not None:
            leaves.setdefault(m.reactions.item.idempotency_key, m.reactions.item.content_hash)
        for fid in () if m.current.deleted else m.current.file_ids:
            f = files[fid]
            leaves.setdefault(f.item.idempotency_key, f.item.content_hash)
    return batch_root(sorted(leaves.items()))


def _banners(inp: PreviewInput, day_messages: int) -> list[str]:
    job = inp.job
    out = []
    bad = [u for u in inp.units if u.status == "failed" or u.recon_status in NOT_COMPLETE]
    for u in bad:
        reasons = [*u.gap_reasons, *([f"access lost: {u.access_lost_reason}"]
                                      if u.access_lost_reason else [])]  # fmt: skip
        out.append(el("p", f"NOT COMPLETE for this day: unit {user(u.unit_key)} is"
                           f" {user(u.recon_status)} (status {user(u.status)}). Expected"
                           f" {num(u.expected)}, collected {num(u.collected)}, file gaps"
                           f" {num(u.file_gaps)}"
                           + (f". Reasons: {', '.join(user(r) for r in reasons)}" if reasons else ""),
                      **{"class": "banner notclean"}))  # fmt: skip
    if not inp.units:
        out.append(el("p", "UNKNOWN (not recorded): no unit of the job covers this day",
                      **{"class": "banner notclean"}))  # fmt: skip
    if job.basis == "archive":
        out.append(el("p", "COMPLETE RELATIVE TO THE PROVIDED EXPORT ONLY. " + escape(ARCHIVE_CAVEAT),
                      **{"class": "banner notclean"}))  # fmt: skip
    if not job.clean:
        out.append(el("p", f"The job is NOT clean: status {user(job.status)}",
                      **{"class": "banner notclean"}))  # fmt: skip
    elif not bad and inp.units:
        out.append(el("p", f"Job status {user(job.status)}", **{"class": "banner clean"}))
    if day_messages == 0:
        statuses = ", ".join(
            f"{user(u.recon_status)}, expected {num(u.expected)}" for u in inp.units
        )
        out.append(
            el("p", f"0 messages (unit status: {statuses or 'none'})", **{"class": "banner"})
        )
    return out


def render_preview(inp: PreviewInput, page: int, cap: int = PREVIEW_PAGE_EVENTS) -> PreviewPage:
    """One page (1-based) of the day. Raises `PreviewPageError` for a page the day does not have,
    `RenderInputError` for inconsistent input."""
    s = inp.slice
    zone = load_zone(inp.time_zone)
    pages = plan_pages(s, cap)
    if not 1 <= page <= len(pages):
        raise PreviewPageError(f"page {page} of {len(pages)}")
    _, lookup = _lookup(s)
    primaries = pages[page - 1]
    on_page = {m.ts for m in primaries}
    context: dict[str, Message] = {}
    for m in primaries:
        root = _root_of(m)
        if root is not None and root not in on_page and root in lookup:
            context.setdefault(root, lookup[root])
    start, end = slice_bounds(s.day, zone)
    everything = sorted([*primaries, *context.values()], key=event_order)
    on_page_all = on_page | set(context)
    events = []
    for m in everything:
        marker_text = None
        if m.ts in context:
            marker_text = ("context: thread root, in the collection (another page or day)"
                           if m.in_scope else "context, outside the collection scope")  # fmt: skip
        events.append(_event(m, s, inp.time_zone, end, context=marker_text, on_page=on_page_all,
                             lookup=lookup))  # fmt: skip
    day_messages = sum(len(p) for p in pages)
    source_hash = _leaves(everything, s.files)
    conv = s.conversation
    title = (
        f"{conv.name or conv.id} {s.day.isoformat()} ({inp.time_zone}) page {page} of {len(pages)}"
    )
    unit_rows = "".join(
        "<tr>" + "".join(el("td", c) for c in (
            user(u.unit_key), user(u.status), user(u.recon_status), num(u.expected),
            num(u.collected), num(u.file_gaps), user(u.access_lost_reason),
            ", ".join(user(r) for r in u.gap_reasons))) + "</tr>"
        for u in inp.units
    )  # fmt: skip
    heads = ("unit", "status", "recon status", "expected", "collected", "file gaps",
             "access lost", "gap reasons")  # fmt: skip
    header = [
        el("h1", "Preview: " + user(title)),
        *_banners(inp, day_messages),
        el(
            "table",
            "".join(
                [
                    "<tr><th>job</th><td>" + user(inp.job.job_id) + "</td></tr>",
                    "<tr><th>job status</th><td>" + user(inp.job.status) + "</td></tr>",
                    "<tr><th>conversation</th><td>"
                    + user(conv.name)
                    + " ("
                    + user(conv.id)
                    + ")</td></tr>",
                    "<tr><th>day</th><td>"
                    + escape(s.day.isoformat())
                    + " ("
                    + escape(inp.time_zone)
                    + "), "
                    + escape(format_utc(start))
                    + " to "
                    + escape(format_utc(end))
                    + "</td></tr>",
                    "<tr><th>page</th><td>"
                    + f"{page} of {len(pages)}: {len(primaries)} message(s) and"
                    f" {len(context)} context event(s); {day_messages} message(s) on the day</td></tr>",
                    "<tr><th>completeness basis</th><td>" + escape(inp.job.basis) + "</td></tr>",
                ]
            ),
        ),
        el("table", "<tr>" + "".join(el("th", escape(h)) for h in heads) + "</tr>" + unit_rows),
        el(
            "p",
            f"Page identity: slice source hash {escape(source_hash)}; preview renderer"
            f" {escape(PREVIEW_RENDERER_VERSION)}",
            **{"class": "meta"},
        ),
    ]
    body = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f'<meta content="{escape(CSP_META)}" http-equiv="Content-Security-Policy">',
        '<meta content="no-referrer" name="referrer">',
        '<meta content="width=device-width, initial-scale=1" name="viewport">',
        el("title", "Preview"),
        el("style", STYLE),
        "</head>",
        "<body>",
        *header,
        el("ol", "".join(events), **{"class": "events"}),
        "</body>",
        "</html>",
        "",
    ]
    html = "\n".join(body).encode("utf-8")
    identity = {"preview_renderer": PREVIEW_RENDERER_VERSION,
                "unicode": unicodedata.unidata_version,
                "time_zone": inp.time_zone,
                **({"tzdata": TZDATA_VERSION} if inp.time_zone != "UTC" else {})}  # fmt: skip
    return PreviewPage(html=html, page=page, pages=len(pages), events=len(primaries),
                       context_events=len(context), day_messages=day_messages,
                       source_hash=source_hash, identity=identity)  # fmt: skip
