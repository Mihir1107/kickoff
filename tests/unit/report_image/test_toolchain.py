"""The PDF toolchain id inside the report image (ADR 0018 §5.3, §5.4, §5.5)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from scripts.report_font_coverage import compute, lock_fonts, render

from edisc_renderers.report.print_css import print_stylesheets_sha256
from edisc_worker.versions import LAYOUT_PACKAGES, measure, vendored_icc

from .conftest import CHILD_PYTHON, FONT_DIR, toolchain


def test_the_id_is_stable_and_hashes_the_layout_path() -> None:
    tid, ident = toolchain()
    assert len(tid) == 64 and measure(CHILD_PYTHON)[0] == tid  # measured twice, same id
    assert ident["platform"] == "linux/x86_64"
    assert "zlib1g" in ident["debian_packages"] and set(LAYOUT_PACKAGES) == set(
        ident["debian_packages"]
    )
    for k in ("pango", "harfbuzz", "freetype", "fontconfig", "fribidi"):
        assert ident["runtime_libraries"][k]
    assert ident["harfbuzz_subset_used"] is True
    assert ident["print_stylesheet_sha256"] == print_stylesheets_sha256()
    assert ident["icc_srgb2014_sha256"] == hashlib.sha256(vendored_icc()).hexdigest()


def test_the_id_holds_the_worker_interpreter_only() -> None:
    """Installers are excluded (they take no part in rendering) and the test tooling lives in its
    own venv: pytest or pypdf in the id would make the admitted id depend on how it was tested."""
    packages = toolchain()[1]["python_packages"]
    assert packages["weasyprint"] == "70.0" and "pydyf" in packages and "fonttools" in packages
    for name in ("pip", "setuptools", "wheel", "pytest", "pypdf", "hypothesis", "html5lib"):
        assert name not in packages, name


def test_exactly_the_vendored_fonts_are_installed_and_hashed() -> None:
    fonts = toolchain()[1]["fonts"]
    want = sorted(n.replace("-VF.ttf", "-Regular.ttf") for n, _ in lock_fonts())
    assert sorted(fonts) == want  # no Japanese font, nothing else
    for name, sha in lock_fonts():
        if not name.endswith("-VF.ttf"):  # instanced fonts get new bytes (still pinned: in the id)
            assert fonts[name] == sha, name
    listed = subprocess.run(["fc-list", "--format", "%{file}\n"], capture_output=True, text=True,
                            check=True).stdout.split()  # fmt: skip
    assert sorted(listed) == sorted(str(FONT_DIR / n) for n in want)  # fontconfig sees only these


def test_the_committed_coverage_is_the_installed_fonts_coverage() -> None:
    """The HTML builder's glyph-coverage set (§5.5) equals the cmaps of the fonts actually installed
    (instanced variable fonts included)."""
    from importlib import resources

    committed = (resources.files("edisc_renderers.report") / "fonts" / "coverage.txt").read_text()
    assert committed == render(compute(FONT_DIR), lock_fonts())


def test_the_bundled_icc_is_the_vendored_profile() -> None:
    from edisc_worker.versions import bundled_icc_path

    assert Path(bundled_icc_path()).read_bytes() == vendored_icc()


def test_the_worker_starts_from_its_production_environment() -> None:
    """The worker venv has production dependencies only: every module the report worker loads must
    import there (a dev-only import once kept the worker from starting outside the dev env)."""
    code = "import edisc_worker.__main__, edisc_worker.reports, edisc_worker.report_pdf"
    subprocess.run([CHILD_PYTHON, "-c", code], check=True, capture_output=True)
