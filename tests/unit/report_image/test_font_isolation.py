"""Fonts are isolated (ADR 0018 §5.4, the S1 leak test): a probe font covering U+10000 (which no
vendored font covers) installed in every default fontconfig location, plus a raw document still
holding U+10000 -> the PDF is byte-identical and embeds no probe font. Control: a configuration that
also scans those directories DOES embed it, so the check can fail. Runs last in the image (it
installs the probe font system-wide)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import pdfcheck
from .conftest import render

PROBE = Path("/opt/edisc/leak/NotoSansLinearB-Regular.ttf")
RAW = ('<!doctype html><html><head><meta charset="utf-8"><title>t</title></head><body>'
       '<p style="font-family: \'Noto Sans\'">Linear B \U00010000 raw</p></body></html>').encode()  # fmt: skip


def test_no_font_outside_the_vendored_dir_reaches_the_pdf(tmp_path: Path) -> None:
    before = render(RAW)
    targets = [Path("/usr/share/fonts/truetype/leak"), Path("/usr/local/share/fonts"),
               Path.home() / ".fonts", Path.home() / ".local/share/fonts", tmp_path / "xdg/fonts"]  # fmt: skip
    for t in targets:
        t.mkdir(parents=True, exist_ok=True)
        shutil.copy(PROBE, t / PROBE.name)
    system = {k: v for k, v in os.environ.items() if k != "FONTCONFIG_FILE"}
    subprocess.run(["fc-cache", "-f"], env=system, check=False, capture_output=True)
    env = {"PATH": os.environ["PATH"], "FONTCONFIG_FILE": os.environ["FONTCONFIG_FILE"],
           "XDG_DATA_HOME": str(tmp_path / "xdg"), "HOME": str(Path.home())}  # fmt: skip
    after = render(RAW, env=env)
    assert after == before
    assert not any("Linear" in f for f in pdfcheck.fonts(pdfcheck.reader(after)))

    leaky = tmp_path / "leaky.conf"
    leaky.write_text('<?xml version="1.0"?><fontconfig><dir>/opt/edisc/fonts</dir>'
                     "<dir>/usr/share/fonts</dir><dir>/usr/local/share/fonts</dir>"
                     f"<cachedir>{tmp_path}/leaky-cache</cachedir></fontconfig>")  # fmt: skip
    control = render(RAW, env={**env, "FONTCONFIG_FILE": str(leaky)})
    assert any("Linear" in f for f in pdfcheck.fonts(pdfcheck.reader(control)))
