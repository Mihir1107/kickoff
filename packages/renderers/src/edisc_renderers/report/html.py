"""The collection report as static HTML (ADR 0018 §3.2, §4), rendered from `report_document`.

Pure (no DB, S3 or clock; the purity test covers it) and byte-identical for the same document and
this renderer version: a fixed `<!doctype html>`, UTF-8, one constant inline `<style>` (its SHA-256 in
the page's CSP), attributes in a fixed order, LF, our own integer formatting (no locale). No report
id, no generation time, no host name -- the times shown are the recorded ones already in the document.

No template engine: a small element builder whose ONLY text path is `escape`. Every user string (a
conversation or custodian name, an error text, an actor, a scope id, a divergence value) is made SAFE
and VISIBLE (`user`):
- a code-point **marker** is an ELEMENT, `<span class="cp">U+XXXX</span>`, styled as a white-on-dark
  box (on screen and in print), never text: a user string that literally reads "[U+202E]" stays
  escaped text and cannot look like, or parse as, a marker (fix 1 before step 3);
- REPLACED by the marker (the character itself is dropped): **bidi controls** (U+061C, U+200E/F,
  U+202A-U+202E, U+2066-U+2069: kept, they reorder the text around them and the marker too; S1
  rendered `‮evil.exe‬` as `exe.live[E202+U]`), and every code point **invalid in HTML**: NUL and
  the other C0 controls except tab, LF and CR, DEL and the C1 controls, noncharacters (U+FDD0-U+FDEF
  and every U+xFFFE/U+xFFFF), and lone surrogates (a `str` can hold one, e.g. from `\\ud800` in
  escaped JSON; UTF-8 cannot encode it);
- KEPT with the marker after them: tab, LF, CR and the other `Cf`, `Zl`, `Zp` characters
  (zero-width space, ZWJ, BOM, ...);
- the text around the markers is HTML-escaped, and the whole value is wrapped in `<bdi>` so a
  right-to-left run cannot reorder the cells around it.
Glyph coverage (replacing characters no vendored font draws) is a PDF concern and lives in step 3; the
HTML keeps every covered character as itself.
"""

from __future__ import annotations

import base64
import hashlib
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from edisc_renderers.report.version import REPORT_RENDERER_VERSION

# bidi controls: ALEF (U+061C), LRM/RLM, the embeddings/overrides U+202A-U+202E, the isolates
# U+2066-U+2069. Not U+202F (narrow no-break space) or U+206A+ (deprecated, caught as Cf below).
BIDI_CONTROLS = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})
_REVEAL_CATEGORIES = frozenset({"Cf", "Cc", "Zl", "Zp"})
_HTML_WHITESPACE_CONTROLS = frozenset({0x09, 0x0A, 0x0D})  # the only C0 controls HTML text keeps


def invalid_in_html(cp: int) -> bool:
    """A code point an HTML document must not contain (a parse error, or not encodable in UTF-8)."""
    return (
        (cp < 0x20 and cp not in _HTML_WHITESPACE_CONTROLS)  # NUL and C0 (form feed included)
        or 0x7F <= cp <= 0x9F  # DEL and C1
        or 0xD800 <= cp <= 0xDFFF  # a lone surrogate
        or 0xFDD0 <= cp <= 0xFDEF  # noncharacters
        or (cp & 0xFFFE) == 0xFFFE  # U+xFFFE / U+xFFFF of every plane
    )


def replaced(cp: int) -> bool:
    """Characters dropped and shown only as their marker (§3.2)."""
    return cp in BIDI_CONTROLS or invalid_in_html(cp)


def marker(cp: int) -> str:
    """The marker ELEMENT for a code point: ASCII only (a vendored font always covers it)."""
    return f'<span class="cp">U+{cp:04X}</span>'


def reveal(text: str) -> str:
    """``text`` as safe HTML (§3.2): escaped text runs, and a marker element for every invisible,
    reordering or HTML-invalid character (replacing it, or after it when it is kept)."""
    out: list[str] = []
    run: list[str] = []
    for ch in text:
        cp = ord(ch)
        if replaced(cp):
            out.append(escape("".join(run)))
            run.clear()
            out.append(marker(cp))  # replaced: a marker after it would itself be reordered
        elif unicodedata.category(ch) in _REVEAL_CATEGORIES:
            run.append(ch)
            out.append(escape("".join(run)))
            run.clear()
            out.append(marker(cp))  # kept, with the marker after it
        else:
            run.append(ch)
    out.append(escape("".join(run)))
    return "".join(out)


def escape(text: str) -> str:
    """The only text path. Escapes the five HTML-significant characters (quotes too, for attributes)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def user(value: Any) -> str:
    """A user-controlled value, made safe and visible and isolated: revealed, escaped, `<bdi>`."""
    return f"<bdi>{reveal('' if value is None else str(value))}</bdi>"


def num(value: Any) -> str:
    """Our own integer formatting, no locale (so bytes do not depend on the environment)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return user(value)


STYLE = (
    "html{font-family:system-ui,sans-serif;font-size:14px;color:#111;background:#fff;margin:0}"
    "body{max-width:60rem;margin:0 auto;padding:1rem}"
    "h1{font-size:1.4rem}h2{font-size:1.1rem;margin:1.4rem 0 .3rem;border-bottom:1px solid #ccc}"
    ".banner{border:2px solid #000;padding:.6rem .8rem;margin:.6rem 0;font-weight:700}"
    ".banner.notclean{background:#fff0f0;border-color:#a00}"
    ".banner.clean{background:#f0fff0;border-color:#0a0}"
    "table{border-collapse:collapse;width:100%;margin:.3rem 0;table-layout:fixed}"
    "th,td{border:1px solid #bbb;padding:2px 5px;text-align:left;vertical-align:top;"
    "overflow-wrap:anywhere}"
    "th{background:#f3f3f3}.src{color:#666;font-weight:400;font-size:.85em}"
    ".more{color:#a00;font-style:italic}bdi{unicode-bidi:isolate}"
    # a code-point marker: an element, never text, so it cannot be confused with user text that
    # reads "[U+202E]"; a white-on-dark box, isolated and left-to-right inside any run
    ".cp{font-family:monospace;font-size:.78em;font-weight:400;font-style:normal;color:#fff;"
    "background:#5a0a8a;border-radius:2px;padding:0 2px;margin:0 1px;white-space:nowrap;"
    "unicode-bidi:isolate;direction:ltr}"
)
STYLE_SHA256 = hashlib.sha256(STYLE.encode("utf-8")).digest()
STYLE_CSP_HASH = f"sha256-{base64.b64encode(STYLE_SHA256).decode('ascii')}"
# a stored page with no external resources: everything is denied but its own inline style
CSP = (
    f"default-src 'none'; style-src '{STYLE_CSP_HASH}'; img-src 'none'; base-uri 'none'; "
    "form-action 'none'"
)


# ------------------------------------------------------------------ element builder
def _attrs(attrs: Mapping[str, str]) -> str:
    return "".join(f' {k}="{escape(v)}"' for k, v in sorted(attrs.items()))


def el(tag: str, inner: str = "", **attrs: str) -> str:
    return f"<{tag}{_attrs(attrs)}>{inner}</{tag}>"


def row(cells: Sequence[str], *, head: bool = False) -> str:
    tag = "th" if head else "td"
    return "<tr>" + "".join(el(tag, c) for c in cells) + "</tr>"


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    body = row([escape(h) for h in headers], head=True) + "".join(row(r) for r in rows)
    return el("table", body)


def _h2(title: str, source: Any = None) -> str:
    src = el("span", f"source: {escape(str(source))}", **{"class": "src"}) if source else ""
    return el("h2", escape(title) + (f" {src}" if src else ""))


# ------------------------------------------------------------------ sections
def _kv(pairs: Iterable[tuple[str, str]]) -> str:
    return table(["field", "value"], [[escape(k), v] for k, v in pairs])


def _counts_table(rows: Sequence[Mapping[str, Any]]) -> str:
    return table(["value", "count"], [[user(r["value"]), num(r["count"])] for r in rows])


def _capped(section: Mapping[str, Any], columns: Sequence[tuple[str, str]], file_name: str) -> str:
    """A severity-ordered capped list: its rows (worst first, already selected by the model), the
    exact total, and -- when the cap hid some -- where the rest is, by file and SHA-256 (§4.6)."""
    rows = [[user(r.get(key)) for _, key in columns] for r in section.get("rows", [])]
    out = [
        el("p", f"{num(section.get('total', 0))} total; showing the {num(len(rows))} worst"),
        table([h for h, _ in columns], rows),
    ]
    more, more_in = section.get("more", 0), section.get("more_in")
    if more and more_in:
        out.append(
            el(
                "p",
                f"{num(more)} more in {escape(more_in['name'])}, SHA-256 {escape(more_in['sha256'])}",
                **{"class": "more"},
            )
        )
    elif more:
        out.append(el("p", f"{num(more)} more in {escape(file_name)}", **{"class": "more"}))
    return "".join(out)


def _banner(doc: Mapping[str, Any]) -> str:
    clean = bool(doc["job"].get("clean"))
    cls = "banner clean" if clean else "banner notclean"
    lines = "<br>".join(reveal(line) for line in doc.get("banner", []))  # a status is verbatim
    return el("div", lines, **{"class": cls})


def _section_pairs(name: str, section: Mapping[str, Any], skip: Sequence[str] = ()) -> str:
    """A key/value section: every scalar field, user-safe, in the document's order."""
    pairs: list[tuple[str, str]] = []
    for k, v in section.items():
        if k in ("source", *skip):
            continue
        if isinstance(v, (list, dict)):
            pairs.append((k, user(v)))
        elif isinstance(v, bool):
            pairs.append((k, "yes" if v else "no"))
        elif isinstance(v, int):
            pairs.append((k, num(v)))
        else:
            pairs.append((k, user(v)))
    return _h2(name, section.get("source")) + _kv(pairs)


def report_html(doc: Mapping[str, Any]) -> bytes:
    """`report.html` for a `report_document` (§3.2, §4). Banner first, exceptions before the totals,
    every enum value in the count tables, UNKNOWN where nothing was recorded."""
    exc = doc["exceptions"]
    unit_cols = [("status", "recon_status"), ("unit", "unit_key"), ("expected", "expected"),
                 ("collected", "collected"), ("error", "error")]  # fmt: skip
    obs_cols = [("kind", "kind"), ("unit", "unit_key"), ("item", "item_id"),
                ("file", "file_id"), ("reason", "reason")]  # fmt: skip
    conv_cols = [("worst status", "worst_status"), ("conversation", "conversation_id"),
                 ("units", "units"), ("expected", "expected"), ("collected", "collected"),
                 ("file gaps", "file_gaps")]  # fmt: skip
    conv = doc.get("conversations") or {}
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f'<meta content="{escape(CSP)}" http-equiv="Content-Security-Policy">',
        '<meta content="width=device-width, initial-scale=1" name="viewport">',
        el("title", "Collection report"),
        el("style", STYLE),
        "</head>",
        "<body>",
        el("h1", "Collection report"),
        _banner(doc),
        _section_pairs("Job", doc["job"]),
        _section_pairs("Access", doc["access"], skip=("database", "export")),
        _h2("Exceptions", exc.get("source")),
        el("p", "Units with exceptions (worst status first):"),
        _capped(exc.get("units", {}), unit_cols, "units.jsonl"),
        el("p", "Item-level observations:"),
        _capped(exc.get("observations_capped", {}), obs_cols, "observations.jsonl"),
        _h2("Counts", doc["counts"].get("source")),
        el("p", "Units by status (every value, zeros included):"),
        _counts_table(doc["counts"]["units_by_status"]),
        el("p", "Units by reconciliation status:"),
        _counts_table(doc["counts"]["units_by_recon_status"]),
        el("p", "Item observations by kind:"),
        _counts_table(doc["exceptions"]["observations"]),
        _h2("Conversations", conv.get("source")),
        el("p", "Per conversation (worst status first):"),
        _capped(conv, conv_cols, "conversations.jsonl"),
        _section_pairs("Pauses", doc["pauses"], skip=("pauses",)),
        _section_pairs("Versions", doc["versions"]),
        _section_pairs("Custody verification", doc["custody_verification"]),
        _section_pairs("Evidence store", doc["evidence_store"], skip=("retention_gaps", "lock")),
        _section_pairs("Renders", doc["renders"]),
        _h2("Divergences", doc["divergences"].get("source")),
        _divergences(doc["divergences"].get("divergences", [])),
        _section_pairs("Integrity", doc["integrity"], skip=("files",)),
        _integrity_files(doc["integrity"].get("files", [])),
        el("p", f"report renderer {escape(REPORT_RENDERER_VERSION)}", **{"class": "src"}),
        "</body>",
        "</html>",
    ]
    return "\n".join(parts).encode("utf-8")


def _divergences(divs: Sequence[Mapping[str, Any]]) -> str:
    if not divs:
        return el("p", "None.")
    rows = [[user(d.get("kind")), user(d.get("subject")), user(d.get("chain")), user(d.get("database"))]
            for d in divs]  # fmt: skip
    return table(["kind", "subject", "chain says", "database says"], rows)


def _integrity_files(files: Sequence[Mapping[str, Any]]) -> str:
    rows = [[user(f.get("name")), num(f.get("rows", 0)), num(f.get("size", 0)), user(f.get("sha256"))]
            for f in files]  # fmt: skip
    return table(["file", "rows", "bytes", "sha256"], rows)
