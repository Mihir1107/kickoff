"""Image build step: fetch every file in fonts.lock, verify its SHA-256, place it, and turn every
variable font (*-VF.ttf: Noto Emoji, the CJK fonts) into a static Regular instance (WeasyPrint/Pango then see an ordinary font).

Layout: /opt/edisc/fonts (the ONLY font dir fontconfig sees), /opt/edisc/leak (the probe font, never
vendored), /opt/edisc/licenses.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path("/opt/edisc")


def main() -> None:
    lock = Path(sys.argv[1])
    for line in lock.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, sha, url = line.split()
        data = urllib.request.urlopen(url, timeout=120).read()
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            raise SystemExit(f"{name}: sha256 {got} != pinned {sha}")
        dest = ROOT / ("fonts" if "/" not in name else "") / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"ok {name} {sha} {len(data)}")

    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    for vf_path in sorted((ROOT / "fonts").glob("*-VF.ttf")):
        # recalcTimestamp=False: fontTools otherwise writes the current time into head.modified
        vf = TTFont(vf_path, recalcTimestamp=False)
        static = instantiateVariableFont(vf, {"wght": 400}, updateFontNames=True)
        out = vf_path.with_name(vf_path.name.replace("-VF.ttf", "-Regular.ttf"))
        static.save(out)
        vf_path.unlink()
        print(f"instanced {out.name} {hashlib.sha256(out.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
