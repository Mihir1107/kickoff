"""The PDF toolchain id (ADR 0018 §5.3): SHA-256 over the RFC 8785 canonical JSON of everything on the
PDF layout path, computed by the interpreter that renders (the report image's
`/opt/edisc/venv/bin/python`). From the spike's `toolchain` command (`spikes/m16-pdf/spike.py`).

    python -m edisc_worker.versions                 # the manifest and the id, as JSON
    python -m edisc_worker.versions --informative   # plus the SHA-256 of every shared library mapped
                                                    # after a render (NOT part of the id, §5.3)

The id covers: platform, Python and Unicode versions; every installed Python distribution and version
except installers (`pip`, `setuptools`, `wheel`: they take no part in rendering); the runtime
versions Pango, HarfBuzz, FreeType, fontconfig and FriBidi report; the Debian package versions of
everything on the layout path (zlib included: it makes the compressed streams, §5.6); whether
HarfBuzz-subset is used; the SHA-256 of every vendored font, of `fonts.conf`, of the print stylesheet
and of the sRGB ICC profile. The paper size is NOT in it (it is in the report identity, §5.7).

Outside the report image there is no toolchain: `report_toolchain_id` returns `none` in
local/test/ci (reports without a PDF, never in production) and raises anywhere else.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import unicodedata
from collections.abc import Mapping
from importlib import metadata, resources, util
from pathlib import Path
from typing import Any

from edisc_core.canonical import canonical_json

NO_TOOLCHAIN = "none"
INSTALLERS = frozenset({"pip", "setuptools", "wheel"})
LAYOUT_PACKAGES = (
    "libpango-1.0-0", "libpangoft2-1.0-0", "libharfbuzz0b", "libharfbuzz-subset0", "libfreetype6",
    "libfontconfig1", "fontconfig-config", "libfribidi0", "libglib2.0-0", "libthai0", "libdatrie1",
    "libgraphite2-3", "libpng16-16", "libbrotli1", "zlib1g", "libexpat1", "libffi8",
)  # fmt: skip


class ToolchainError(RuntimeError):
    """The PDF toolchain is missing or inconsistent: this process must not produce report PDFs."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vendored_icc() -> bytes:
    return (resources.files("edisc_renderers.report") / "icc" / "sRGB2014.icc").read_bytes()


def bundled_icc_path() -> Path:
    """WeasyPrint's own copy (it embeds THAT one as the output intent), found without importing it."""
    spec = util.find_spec("weasyprint")
    if spec is None or not spec.submodule_search_locations:
        raise ToolchainError("weasyprint is not installed")
    return Path(next(iter(spec.submodule_search_locations))) / "pdf" / "sRGB2014.icc"


def check_icc() -> str:
    """The vendored sRGB2014 profile and WeasyPrint's bundled copy must be byte-equal (§5.6)."""
    vendored = vendored_icc()
    bundled = bundled_icc_path().read_bytes()
    if bundled != vendored:
        raise ToolchainError(
            "WeasyPrint's bundled sRGB2014.icc differs from the vendored profile"
            f" ({hashlib.sha256(bundled).hexdigest()} != {hashlib.sha256(vendored).hexdigest()})"
        )
    return hashlib.sha256(vendored).hexdigest()


def python_packages() -> dict[str, str]:
    out = {}
    for d in metadata.distributions():
        name = str(d.metadata["Name"]).lower()
        if name not in INSTALLERS:
            out[name] = d.version
    return dict(sorted(out.items()))


def runtime_libraries() -> dict[str, str]:
    pango = ctypes.CDLL("libpango-1.0.so.0")
    pango.pango_version_string.restype = ctypes.c_char_p
    hb = ctypes.CDLL("libharfbuzz.so.0")
    hb.hb_version_string.restype = ctypes.c_char_p
    fc = ctypes.CDLL("libfontconfig.so.1")
    v = fc.FcGetVersion()
    ft = ctypes.CDLL("libfreetype.so.6")
    handle = ctypes.c_void_p()
    ft.FT_Init_FreeType(ctypes.byref(handle))
    a, b, c = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    ft.FT_Library_Version(handle, ctypes.byref(a), ctypes.byref(b), ctypes.byref(c))
    fribidi = ctypes.CDLL("libfribidi.so.0")
    fb = ctypes.c_char_p.in_dll(fribidi, "fribidi_version_info").value or b""
    return {
        "pango": pango.pango_version_string().decode(),
        "harfbuzz": hb.hb_version_string().decode(),
        "fontconfig": f"{v // 10000}.{v // 100 % 100}.{v % 100}",
        "freetype": f"{a.value}.{b.value}.{c.value}",
        "fribidi": fb.decode().splitlines()[0],
    }


def harfbuzz_subset_used() -> bool:
    """WeasyPrint subsets fonts with HarfBuzz-subset when the library loads (it does in the image)."""
    try:
        ctypes.CDLL("libharfbuzz-subset.so.0")
    except OSError:
        return False
    return True


def debian_packages() -> dict[str, str]:
    out = subprocess.run(  # noqa: S603  (fixed argv)
        ["dpkg-query", "-W", "-f", "${Package} ${Version}\n", *LAYOUT_PACKAGES],  # noqa: S607
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    found = dict(line.split(" ", 1) for line in out.strip().splitlines())
    missing = [p for p in LAYOUT_PACKAGES if p not in found]
    if missing:
        raise ToolchainError(f"layout packages not installed: {missing}")
    return found


def manifest(font_dir: Path, fonts_conf: Path) -> dict[str, Any]:
    """The identity the toolchain id hashes (§5.3). Raises ToolchainError outside a report image."""
    from edisc_renderers.report.print_css import print_stylesheets_sha256

    fonts = sorted(font_dir.glob("*.ttf"))
    if not fonts:
        raise ToolchainError(f"no vendored fonts in {font_dir}")
    return {
        "platform": f"{platform.system().lower()}/{platform.machine()}",
        "python": platform.python_version(),
        "unicode": unicodedata.unidata_version,
        "python_packages": python_packages(),
        "runtime_libraries": runtime_libraries(),
        "debian_packages": debian_packages(),
        "harfbuzz_subset_used": harfbuzz_subset_used(),
        "fonts": {f.name: sha256_file(f) for f in fonts},
        "fonts_conf_sha256": sha256_file(fonts_conf),
        "print_stylesheet_sha256": print_stylesheets_sha256(),
        "icc_srgb2014_sha256": check_icc(),
    }


def toolchain_id(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def from_environment() -> tuple[str, dict[str, Any]]:
    """The id and manifest of THIS interpreter, with the font dir and fontconfig of its environment
    (`EDISC_REPORT_FONT_DIR`, `FONTCONFIG_FILE`, both set by the report image)."""
    font_dir, conf = os.environ.get("EDISC_REPORT_FONT_DIR"), os.environ.get("FONTCONFIG_FILE")
    if not font_dir or not conf:
        raise ToolchainError("not a report image: EDISC_REPORT_FONT_DIR / FONTCONFIG_FILE unset")
    try:
        identity = manifest(Path(font_dir), Path(conf))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ToolchainError(f"PDF toolchain unavailable: {exc}") from exc
    return toolchain_id(identity), identity


def measure(python: str, timeout: float = 120.0) -> tuple[str, dict[str, Any]]:
    """The toolchain id of the interpreter the PDF child runs (``python -m edisc_worker.versions``),
    from the outside: the worker never imports WeasyPrint itself."""
    proc = subprocess.run(  # noqa: S603  (our own interpreter and module)
        [python, "-m", "edisc_worker.versions"], capture_output=True, text=True, timeout=timeout,
        check=False,
    )  # fmt: skip
    if proc.returncode != 0:
        raise ToolchainError(f"toolchain measurement failed: {proc.stderr.strip()[-2000:]}")
    out = json.loads(proc.stdout)
    return str(out["toolchain_id"]), dict(out["identity"])


def report_toolchain_id(env_is_ephemeral: bool, python: str | None) -> str:
    """The toolchain id this worker's PDFs carry. ``none`` only where reports without a PDF are
    allowed (local/test/ci) and there is no report image; anywhere else a missing or inconsistent
    toolchain refuses to start the report worker."""
    try:
        return measure(python or sys.executable)[0]
    except ToolchainError:
        if env_is_ephemeral:
            return NO_TOOLCHAIN
        raise


def _mapped_libraries() -> dict[str, str]:
    paths = sorted(
        {
            line.split()[-1]
            for line in Path("/proc/self/maps").read_text().splitlines()
            if ".so" in line.split()[-1]
        }
    )
    return {p: sha256_file(Path(p)) for p in paths if Path(p).is_file()}


def main(argv: list[str]) -> int:
    try:
        tid, identity = from_environment()
    except ToolchainError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    out: dict[str, Any] = {"toolchain_id": tid, "identity": identity}
    if "--informative" in argv:
        from edisc_worker.report_pdf import render_pdf_bytes

        render_pdf_bytes(b"<!doctype html><title>t</title><p>loads every library</p>", "letter")
        out["informative_mapped_libraries"] = _mapped_libraries()
    sys.stdout.write(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
