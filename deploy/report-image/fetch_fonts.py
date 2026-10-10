"""Report image build step (ADR 0018 §5.4): fetch every file in fonts.lock, verify its SHA-256,
place it, and turn every variable font (*-VF.ttf: Noto Emoji, the CJK fonts) into a static Regular
instance (WeasyPrint/Pango then see an ordinary font). From the spike (`spikes/m16-pdf/`).

Layout under ROOT (argv[2], default /opt/edisc): `fonts/` (the ONLY font dir fontconfig sees),
`leak/` (the probe font of the isolation test, never vendored; only the test image copies it),
`licenses/`.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path


def main() -> None:
    lock = Path(sys.argv[1])
    root = Path(sys.argv[2] if len(sys.argv) > 2 else "/opt/edisc")
    for line in lock.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, sha, url = line.split()
        data = urllib.request.urlopen(url, timeout=120).read()  # noqa: S310 (pinned https URLs)
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            raise SystemExit(f"{name}: sha256 {got} != pinned {sha}")
        dest = root / ("fonts" if "/" not in name else "") / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"ok {name} {sha} {len(data)}")  # noqa: T201 (image build log)

    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    for vf_path in sorted((root / "fonts").glob("*-VF.ttf")):
        # recalcTimestamp=False: fontTools otherwise writes the current time into head.modified
        vf = TTFont(vf_path, recalcTimestamp=False)
        static = instantiateVariableFont(vf, {"wght": 400}, updateFontNames=True)
        out = vf_path.with_name(vf_path.name.replace("-VF.ttf", "-Regular.ttf"))
        static.save(out)
        vf_path.unlink()
        print(f"instanced {out.name} {hashlib.sha256(out.read_bytes()).hexdigest()}")  # noqa: T201


if __name__ == "__main__":
    main()
