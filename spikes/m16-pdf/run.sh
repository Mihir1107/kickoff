#!/usr/bin/env bash
# Runs the S1 checks against an already loaded image on this host. Usage: run.sh OUTDIR [HOST_FONT_DIR]
set -euo pipefail
OUT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1"); mkdir -p "$OUT"
HOST_FONTS=${2:-/usr/share/fonts}
IMG=edisc-pdf-spike:s1
D=(docker run --rm --platform linux/amd64 -v "$OUT":/work)

{
  echo "image_id $(docker image inspect --format '{{.Id}}' $IMG)"
  echo "host $(hostname)"
  echo "host_arch $(uname -m)"
  echo "host_os $(uname -sr)"
  echo "container_cpu $("${D[@]}" --entrypoint sh $IMG -c "grep -m1 'model name' /proc/cpuinfo || uname -m")"
  echo "date $(date -u +%FT%TZ)"
} | tee "$OUT/host.txt"

"${D[@]}" $IMG html letter /work/report.html | tee "$OUT/html.json"
"${D[@]}" $IMG runs 20 /work/runs plain,pdfa2u,compressed | tee "$OUT/runs.json"
"${D[@]}" $IMG inspect /work/runs/plain-00.pdf > "$OUT/inspect-plain.json"
"${D[@]}" $IMG inspect /work/runs/pdfa2u-00.pdf > "$OUT/inspect-pdfa2u.json"
"${D[@]}" $IMG html a4 /work/report-a4.html > "$OUT/html-a4.json"
"${D[@]}" $IMG render plain /work/a4.pdf /work/report-a4.html > "$OUT/a4.json"
"${D[@]}" $IMG toolchain > "$OUT/toolchain.json"
if [ -d "$HOST_FONTS" ]; then
  "${D[@]}" -v "$HOST_FONTS":/usr/share/fonts/host:ro $IMG leak | tee "$OUT/leak.json"
else
  "${D[@]}" $IMG leak | tee "$OUT/leak.json"
fi
( cd "$OUT/runs" && shasum -a 256 *.pdf ) > "$OUT/pdf-sha256.txt"
# PDF/A-2u conformance (recorded; the byte-identity criteria above do not depend on it)
VERAPDF=verapdf/cli:v1.30.2@sha256:d5ee329657cf9bc4b2400392dd54c7d0a0ce9980ff6fa2da5590eebeec007cdb
docker run --rm --platform linux/amd64 -v "$OUT/runs":/data $VERAPDF --flavour 2u --format text \
  /data/pdfa2u-00.pdf > "$OUT/verapdf.txt" 2>/dev/null || true
docker run --rm --platform linux/amd64 -v "$OUT/runs":/data $VERAPDF --flavour 2u --format json \
  /data/pdfa2u-00.pdf > "$OUT/verapdf.json" 2>/dev/null || true
cat "$OUT/verapdf.txt"
echo "S1 checks passed on $(hostname)"
