"""M16 spike S1: is a WeasyPrint PDF byte-identical across runs and hosts in one pinned image?

Commands (run inside the image, see run.sh):
  html                   write the report HTML at the 1,000-row cap (report.html, sizes)
  render VARIANT OUT     render one PDF from report.html (one process = one run)
  runs N OUTDIR          N separate processes per variant with varied env; prints a hash table
  toolchain              the PDF toolchain manifest and its id
  inspect PDF            structural facts (fonts, ids, dates, actions, pages)
  leak                   proves fonts outside /opt/edisc/fonts cannot reach the PDF
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path

FONT_DIR = Path("/opt/edisc/fonts")
LEAK_FONT = Path("/opt/edisc/leak/NotoSansLinearB-Regular.ttf")
SEALED_AT = "2026-10-05T12:34:56Z"  # recorded job seal time: the PDF's only date (D4)
CAP = 1000
FAMILIES = (
    '"Noto Sans", "Noto Sans Arabic", "Noto Sans Hebrew", "Noto Sans Devanagari", "Noto Sans Thai",'
    ' "Noto Sans SC", "Noto Sans JP", "Noto Sans KR", "Noto Emoji"'
)
PAPER = {"letter": "Letter", "a4": "A4"}

# Every hard case the acceptance criteria name, as user strings (names, error texts).
HARD = [
    "张伟（销售部）",  # CJK SC
    "山田太郎・営業",  # JP
    "김민준 팀장",  # KR
    "محمد عبد الله",  # Arabic (RTL, shaping)
    "שרה כהן",  # Hebrew (RTL)
    "Report: تقرير 2026 — שלב 3",  # mixed bidi
    "🎉 launch 👍🏽 🇮🇳",  # emoji, skin tone, flag
    "👩‍👩‍👧‍👦 family",  # ZWJ sequence
    "é̂ Zalgo z̶a̷l̸",  # combining marks
    "नमस्ते क्षत्रिय",  # Devanagari conjuncts
    "สวัสดีครับ",  # Thai
    "zero​width‌joiner﻿bom",  # zero-width characters
    "‮evil.exe‬ override",  # bidi override (spoofing)
    "Linear B \U00010000 and \U00013000",  # covered by NO vendored font
    "plain ASCII channel #general",
]

_COVERAGE: set[int] | None = None


def coverage() -> set[int]:
    global _COVERAGE
    if _COVERAGE is None:
        from fontTools.ttLib import TTFont

        cps: set[int] = set()
        for f in sorted(FONT_DIR.iterdir()):
            cps |= set(TTFont(f, lazy=True).getBestCmap())
        _COVERAGE = cps
    return _COVERAGE


BIDI_CONTROLS = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})


def marker(cp: int) -> str:
    return f"[U+{cp:04X}]"


def visible(s: str) -> str:
    """The report's rule for user strings in the PDF: bidi controls are REPLACED by the marker (kept,
    they reorder the text around them, marker included: S1 round 1 showed "exe.live[E202+U]"); other
    invisible/format/control characters stay and get a visible marker after them; characters no
    vendored font covers are REPLACED by the marker (never tofu, never a silent substitute)."""
    cov = coverage()
    out = []
    for ch in s:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if cp in BIDI_CONTROLS:
            out.append(marker(cp))
        elif cat in ("Cf", "Cc", "Zl", "Zp"):
            out.append(ch + marker(cp))
        elif cp not in cov and not ch.isspace():
            out.append(marker(cp))
        else:
            out.append(ch)
    return "".join(out)


def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def user(s: str, raw: bool) -> str:
    return f"<bdi>{esc(s if raw else visible(s))}</bdi>"


STATUSES = ["failed", "access_lost", "gap", "unverifiable", "surplus", "matched_against_archive"]


def build_html(paper: str = "letter", raw: bool = False) -> str:
    """A report-shaped document at the D5 cap: six capped sections of 1,000 rows, a per-status
    table with every enum value, banner and page furniture on every page."""
    css = (
        f"@page {{ size: {PAPER[paper]}; margin: 22mm 12mm 18mm 12mm;"
        ' @top-center { content: "NOT COMPLETE: completed_with_gaps \\2014  1,204 units with gaps,'
        ' 37 failed"; font: bold 9pt "Noto Sans"; }'
        ' @bottom-left { content: "job 0192f3a4-7c1e-7d2a-9b5e-3f0c2d1e4a5b \\00B7  snapshot 3f9a1c0d";'
        ' font: 7pt "Noto Sans Mono"; }'
        ' @bottom-right { content: "page " counter(page) " of " counter(pages); font: 7pt "Noto Sans"; } }'
        f" body {{ font-family: {FAMILIES}; font-size: 7.5pt; }}"
        " h1 { font-size: 13pt; } h2 { font-size: 10pt; margin: 8pt 0 3pt; }"
        " .banner { border: 2pt solid #000; padding: 4pt; font-weight: bold; }"
        " table { border-collapse: collapse; width: 100%; table-layout: fixed; }"
        " th, td { border: 0.4pt solid #555; padding: 1pt 2pt; vertical-align: top;"
        " overflow-wrap: anywhere; }"
        ' .m { font-family: "Noto Sans Mono"; }'
    )
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>Collection report 0192f3a4</title>",
        f'<meta name="dcterms.created" content="{SEALED_AT}">',
        f'<meta name="dcterms.modified" content="{SEALED_AT}">',
        f"<style>{css}</style></head><body>",
        "<h1>Collection report</h1>",
        '<p class="banner">NOT COMPLETE: completed_with_gaps. 1,204 units with gaps, 37 failed units,'
        " 4,812 unavailable files. Exceptions are listed first; lists are capped at 1,000 rows each,"
        " the full lists are in units.jsonl and observations.jsonl (SHA-256 below).</p>",
        "<h2>Units by status (every value listed)</h2><table><tr><th>status</th><th>units</th></tr>",
    ]
    for i, st in enumerate(
        [
            "matched",
            "gap",
            "surplus",
            "unverifiable",
            "failed",
            "access_lost",
            "not_applicable",
            "matched_against_archive",
            "pending",
        ]
    ):
        parts.append(f"<tr><td class=m>{st}</td><td>{(i * 7919) % 3001:,}</td></tr>")
    parts.append("</table>")
    sections = [
        ("Failed units", "unit_failed"),
        ("Units with access lost", "access_lost"),
        ("Units with gaps", "gap"),
        ("Unverifiable units", "unverifiable"),
        ("Unavailable files", "file_unavailable"),
        ("Per conversation", "conversation"),
    ]
    for title, kind in sections:
        n_total = CAP + 3812
        parts.append(
            f"<h2>{title}: {n_total:,} (first {CAP:,} shown, worst status first, then by key)</h2>"
        )
        parts.append(
            "<table><tr><th style='width:22%'>unit</th><th style='width:20%'>conversation</th>"
            "<th style='width:9%'>day</th><th style='width:6%'>tz</th><th style='width:10%'>status</th>"
            "<th style='width:6%'>exp.</th><th style='width:6%'>coll.</th><th>reason</th></tr>"
        )
        for r in range(CAP):
            name = HARD[(r + len(kind)) % len(HARD)]
            reason = (
                HARD[(r * 7 + 3) % len(HARD)] if r % 5 == 0 else f"{kind}: HTTP 429 after 6 retries"
            )
            parts.append(
                f"<tr><td class=m>C0{r:07d}/2026-{1 + r % 12:02d}-{1 + r % 28:02d}</td>"
                f"<td>{user(name, raw)}</td><td class=m>2026-{1 + r % 12:02d}-{1 + r % 28:02d}</td>"
                f"<td class=m>UTC</td><td class=m>{STATUSES[r % len(STATUSES)]}</td>"
                f"<td>{100 + r % 37}</td><td>{97 + r % 37}</td><td>{user(reason, raw)}</td></tr>"
            )
        parts.append(
            f"<tr><td colspan=8><b>{n_total - CAP:,} more rows not shown:</b> units.jsonl"
            " sha256 9c1d…e04f</td></tr></table>"
        )
    parts.append("<h2>Hard strings (verbatim in report.json)</h2><table>")
    for s in HARD:
        parts.append(
            f"<tr><td>{user(s, raw)}</td><td class=m>{esc(s.encode('unicode_escape').decode())}</td></tr>"
        )
    parts.append("</table></body></html>\n")
    return "\n".join(parts)


def render(html: bytes, variant: str) -> bytes:
    from weasyprint import HTML

    ident = hashlib.sha256(html).digest()[:16]  # /ID from the HTML bytes, never random
    opts: dict[str, object] = {"pdf_identifier": ident, "uncompressed_pdf": True}
    if variant == "pdfa2u":
        opts["pdf_variant"] = "pdf/a-2u"
    elif variant == "compressed":
        opts["uncompressed_pdf"] = False
    elif variant != "plain":
        raise SystemExit(f"unknown variant {variant}")
    return HTML(string=html.decode()).write_pdf(**opts)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def cmd_html(args: list[str]) -> None:
    paper = args[0] if args else "letter"
    out = Path(args[1] if len(args) > 1 else "report.html")
    raw = len(args) > 2 and args[2] == "raw"
    data = build_html(paper, raw).encode()
    out.write_bytes(data)
    print(json.dumps({"html": str(out), "bytes": len(data), "sha256": sha(data)}))


def cmd_render(args: list[str]) -> None:
    variant, out = args[0], Path(args[1])
    src = Path(args[2] if len(args) > 2 else "report.html").read_bytes()
    t0 = time.monotonic()
    pdf = render(src, variant)
    out.write_bytes(pdf)
    print(
        json.dumps(
            {
                "variant": variant,
                "bytes": len(pdf),
                "sha256": sha(pdf),
                "seconds": round(time.monotonic() - t0, 2),
                "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
            }
        )
    )


ENVS = [
    {"TZ": "UTC", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"},
    {"TZ": "Asia/Kolkata", "LANG": "C", "PYTHONHASHSEED": "random"},
    {"TZ": "America/New_York", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "12345"},
    {"TZ": "Asia/Kathmandu", "LANG": "POSIX", "PYTHONHASHSEED": "random"},
]


def cmd_runs(args: list[str]) -> None:
    n, outdir = int(args[0]), Path(args[1])
    variants = args[2].split(",") if len(args) > 2 else ["plain", "pdfa2u", "compressed"]
    outdir.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict[str, object]]] = {v: [] for v in variants}
    jobs = []
    for i in range(n):
        env = {**os.environ, **ENVS[i % len(ENVS)], "HOME": f"/tmp/home{i}"}
        Path(env["HOME"]).mkdir(exist_ok=True)
        jobs.extend((v, i, env) for v in variants)

    def one(job: tuple[str, int, dict[str, str]]) -> tuple[str, dict[str, object]]:
        v, i, env = job
        out = outdir / f"{v}-{i:02d}.pdf"
        p = subprocess.run(
            [sys.executable, __file__, "render", v, str(out)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return v, json.loads(p.stdout.strip().splitlines()[-1])

    # separate processes (one render each), a few at a time
    with ThreadPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 1)))) as pool:
        for v, r in pool.map(one, jobs):
            results[v].append(r)
    summary = {}
    for v, rs in results.items():
        hashes = sorted({str(r["sha256"]) for r in rs})
        summary[v] = {
            "runs": len(rs),
            "distinct_sha256": hashes,
            "identical": len(hashes) == 1,
            "bytes": rs[0]["bytes"],
            "seconds_max": max(float(str(r["seconds"])) for r in rs),
            "peak_rss_mb_max": max(int(str(r["peak_rss_mb"])) for r in rs),
        }
    print(json.dumps(summary, indent=1))
    (outdir / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    if not all(s["identical"] for s in summary.values()):
        raise SystemExit("NOT IDENTICAL")


LAYOUT_PACKAGES = [
    "libpango-1.0-0",
    "libpangoft2-1.0-0",
    "libharfbuzz0b",
    "libharfbuzz-subset0",
    "libfreetype6",
    "libfontconfig1",
    "fontconfig-config",
    "libfribidi0",
    "libglib2.0-0",
    "libthai0",
    "libdatrie1",
    "libgraphite2-3",
    "libpng16-16",
    "libbrotli1",
    "zlib1g",
    "libexpat1",
    "libffi8",
]


def runtime_versions() -> dict[str, str]:
    def lib(name: str) -> ctypes.CDLL:
        return ctypes.CDLL(name)

    pango = lib("libpango-1.0.so.0")
    pango.pango_version_string.restype = ctypes.c_char_p
    hb = lib("libharfbuzz.so.0")
    hb.hb_version_string.restype = ctypes.c_char_p
    fc = lib("libfontconfig.so.1")
    v = fc.FcGetVersion()
    ft = lib("libfreetype.so.6")
    handle = ctypes.c_void_p()
    ft.FT_Init_FreeType(ctypes.byref(handle))
    a, b, c = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    ft.FT_Library_Version(handle, ctypes.byref(a), ctypes.byref(b), ctypes.byref(c))
    fribidi = lib("libfribidi.so.0")
    fb = ctypes.c_char_p.in_dll(fribidi, "fribidi_version_info").value or b""
    return {
        "pango": pango.pango_version_string().decode(),
        "harfbuzz": hb.hb_version_string().decode(),
        "fontconfig": f"{v // 10000}.{v // 100 % 100}.{v % 100}",
        "freetype": f"{a.value}.{b.value}.{c.value}",
        "fribidi": fb.decode().splitlines()[0],
    }


def dpkg_versions() -> dict[str, str]:
    out = subprocess.run(
        ["dpkg-query", "-W", "-f", "${Package} ${Version}\n", *LAYOUT_PACKAGES],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(line.split(" ", 1) for line in out.strip().splitlines())


def mapped_libraries() -> dict[str, str]:
    paths = sorted(
        {
            line.split()[-1]
            for line in Path("/proc/self/maps").read_text().splitlines()
            if ".so" in line.split()[-1]
        }
    )
    return {p: sha(Path(p).read_bytes()) for p in paths if Path(p).is_file()}


def cmd_toolchain(_: list[str]) -> None:
    import weasyprint

    small = f"<html><body style='font-family: {FAMILIES}'>{''.join(f'<p>{user(s, False)}</p>' for s in HARD)}"
    render(small.encode(), "plain")  # loads every library a real render loads
    icc = Path(weasyprint.__file__).parent / "pdf" / "sRGB2014.icc"
    identity = {
        "platform": f"{platform.system().lower()}/{platform.machine()}",
        "python": platform.python_version(),
        "unicode": unicodedata.unidata_version,
        "python_packages": {
            d.metadata["Name"].lower(): d.version for d in metadata.distributions()
        },
        "runtime_libraries": runtime_versions(),
        "debian_packages": dpkg_versions(),
        "harfbuzz_subset_used": True,  # libharfbuzz-subset0 present and HarfBuzz >= 4.1 (fonts.py)
        "fonts": {f.name: sha(f.read_bytes()) for f in sorted(FONT_DIR.iterdir())},
        "fonts_conf_sha256": sha(Path(os.environ["FONTCONFIG_FILE"]).read_bytes()),
        "icc_srgb2014_sha256": sha(icc.read_bytes()),
        "fontconfig_env": {k: os.environ.get(k) for k in ("FONTCONFIG_FILE", "FONTCONFIG_PATH")},
    }
    identity["python_packages"] = dict(sorted(identity["python_packages"].items()))
    canon = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    print(
        json.dumps(
            {
                "toolchain_id": sha(canon),
                "identity": identity,
                "informative_mapped_libraries": mapped_libraries(),
            },
            indent=1,
            sort_keys=True,
        )
    )


def embedded_fonts(path: Path) -> list[str]:
    """BaseFont names of every font the PDF's pages use (subset tag included)."""
    from pypdf import PdfReader

    fonts: set[str] = set()
    for page in PdfReader(path).pages:
        res = page.get("/Resources") or {}
        for f in (res.get("/Font") or {}).values():
            fd = f.get_object()
            base = str(fd.get("/BaseFont"))
            for d in fd.get("/DescendantFonts") or []:
                base = str(d.get_object().get("/BaseFont"))
            fonts.add(base)
    return sorted(fonts)


def cmd_inspect(args: list[str]) -> None:
    from pypdf import PdfReader

    data = Path(args[0]).read_bytes()
    r = PdfReader(Path(args[0]))
    fonts = embedded_fonts(Path(args[0]))
    first = r.pages[0].extract_text() or ""
    last = r.pages[-1].extract_text() or ""
    banned = [
        k
        for k in (b"/JavaScript", b"/OpenAction", b"/AA", b"/Launch", b"/URI", b"/EmbeddedFile")
        if k in data
    ]
    print(
        json.dumps(
            {
                "pages": len(r.pages),
                "bytes": len(data),
                "info": {k: str(v) for k, v in (r.metadata or {}).items()},
                "trailer_id": [
                    (getattr(x, "original_bytes", None) or str(x).encode("latin-1")).hex()
                    for x in r.trailer.get("/ID", [])
                ],
                "fonts": sorted(fonts),
                "banned_keys_present": banned,
                "banner_on_first_page": "NOT COMPLETE" in first,
                "banner_on_last_page": "NOT COMPLETE" in last,
                "footer_last": "of " + str(len(r.pages)) in last,
                "marker_present": "[U+10000]" in (r.pages[-1].extract_text() or "") + first,
            },
            indent=1,
            ensure_ascii=False,
        )
    )


def cmd_leak(_: list[str]) -> None:
    """1) fc-list under our config = exactly the vendored files; 2) a raw document holding U+10000
    (no vendored glyph) renders identically after the probe font is installed everywhere a default
    fontconfig looks (system, local, home, XDG) and after host fonts are mounted; 3) positive
    control: a config that also scans those dirs DOES pick the probe font (the check can fail)."""
    work = Path("/tmp/leak")
    work.mkdir(exist_ok=True)
    listed = subprocess.run(
        ["fc-list", "--format", "%{file}\n"], capture_output=True, text=True, check=True
    ).stdout.split()
    vendored = sorted(str(f) for f in FONT_DIR.iterdir())
    raw_html = build_html(raw=True).encode()
    (work / "raw.html").write_bytes(raw_html)
    before = render(raw_html, "plain")

    targets = [
        Path("/usr/share/fonts/truetype/leak"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
        Path("/tmp/xdg/fonts"),
    ]
    for t in targets:
        t.mkdir(parents=True, exist_ok=True)
        shutil.copy(LEAK_FONT, t / LEAK_FONT.name)
    env_sys = {k: v for k, v in os.environ.items() if k != "FONTCONFIG_FILE"}
    subprocess.run(["fc-cache", "-f"], env=env_sys, check=False, capture_output=True)
    host_mount = Path("/usr/share/fonts/host")
    host_fonts = sum(1 for _ in host_mount.rglob("*") if _.is_file()) if host_mount.exists() else 0
    env_iso = {**os.environ, "XDG_DATA_HOME": "/tmp/xdg"}
    p = subprocess.run(
        [
            sys.executable,
            __file__,
            "render",
            "plain",
            str(work / "after.pdf"),
            str(work / "raw.html"),
        ],
        env=env_iso,
        capture_output=True,
        text=True,
        check=True,
    )
    after = (work / "after.pdf").read_bytes()

    leaky = Path("/tmp/leaky.conf")
    leaky.write_text(
        '<?xml version="1.0"?><fontconfig><dir>/opt/edisc/fonts</dir><dir>/usr/share/fonts</dir>'
        "<dir>/usr/local/share/fonts</dir><dir>~/.fonts</dir><cachedir>/tmp/leaky-cache</cachedir>"
        "</fontconfig>"
    )
    subprocess.run(
        [
            sys.executable,
            __file__,
            "render",
            "plain",
            str(work / "control.pdf"),
            str(work / "raw.html"),
        ],
        env={**os.environ, "FONTCONFIG_FILE": str(leaky)},
        capture_output=True,
        text=True,
        check=True,
    )
    control = (work / "control.pdf").read_bytes()
    result = {
        "fc_list_equals_vendored": sorted(listed) == vendored,
        "fc_list": sorted(listed),
        "host_fonts_mounted": host_fonts,
        "probe_installed_in": [str(t) for t in targets],
        "isolated_identical_after_install": sha(before) == sha(after),
        "isolated_contains_probe_font": any(
            "Linear" in f for f in embedded_fonts(work / "after.pdf")
        ),
        "control_contains_probe_font": any(
            "Linear" in f for f in embedded_fonts(work / "control.pdf")
        ),
        "control_fonts": embedded_fonts(work / "control.pdf"),
        "control_differs": sha(control) != sha(before),
        "render": json.loads(p.stdout.strip().splitlines()[-1]),
    }
    print(json.dumps(result, indent=1))
    ok = (
        result["fc_list_equals_vendored"]
        and result["isolated_identical_after_install"]
        and not result["isolated_contains_probe_font"]
        and result["control_contains_probe_font"]
    )
    if not ok:
        raise SystemExit("LEAK CHECK FAILED")


def main() -> None:
    cmd, *args = sys.argv[1:]
    {
        "html": cmd_html,
        "render": cmd_render,
        "runs": cmd_runs,
        "toolchain": cmd_toolchain,
        "inspect": cmd_inspect,
        "leak": cmd_leak,
    }[cmd](args)


if __name__ == "__main__":
    main()
