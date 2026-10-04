#!/usr/bin/env bash
# Mentor demo (docs/DEMO.md). Usage:
#   scripts/demo.sh        clean start: stack up, seed, export upload, job with a SIGKILL, reconciliation,
#                          custody verify, edisc-verify VERIFIED, flipped byte FAILED, RSMF render
#   scripts/demo.sh down   destroy the demo stack (all its volumes) and demo-output/
# Uses its own compose project (edisc-demo) and ports; never the dev or test stacks.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=edisc-demo
ENV_FILE=.env.demo
OUT=demo-output
API_PORT=18100
MIN_FREE_GB=${MIN_FREE_GB:-10}
COMPOSE=(docker compose -p "$PROJECT" -f infra/docker-compose.yml --env-file "$ENV_FILE")
export EDISC_ENV_FILE=$ENV_FILE EDISC_COMPOSE_PROJECT=$PROJECT

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
teardown() {
  if [ -f "$ENV_FILE" ]; then "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; fi
}

if [ "${1:-run}" = "down" ]; then
  teardown
  rm -rf "$OUT" .local-kms-demo "$ENV_FILE"
  echo "demo stack and output removed"
  exit 0
fi

T0=$(date +%s)
bold "0. Clean start"
free=$(df -Pk . | awk 'NR==2 {print int($4/1024/1024)}')
if [ "$free" -lt "$MIN_FREE_GB" ]; then echo "refusing: ${free} GB free, need ${MIN_FREE_GB}" >&2; exit 1; fi
teardown
rm -rf "$OUT" .local-kms-demo
mkdir -p "$OUT/logs"
uv run --quiet python scripts/demo_env.py > "$ENV_FILE"
echo "   ${free} GB free; previous demo stack and output removed"

bold "1. Stack up: Postgres (RLS), Redis, MinIO (Object Lock), Temporal"
"${COMPOSE[@]}" up -d --wait postgres redis minio temporal temporal-ui >"$OUT/logs/compose.log" 2>&1
for job in minio-init temporal-namespace; do
  "${COMPOSE[@]}" run --rm --no-deps "$job" >>"$OUT/logs/compose.log" 2>&1
done
uv run --quiet python -m edisc_db.bootstrap >"$OUT/logs/migrate.log" 2>&1
uv run --quiet python -m edisc_db.migrate upgrade >>"$OUT/logs/migrate.log" 2>&1
echo "   schema migrated to $("${COMPOSE[@]}" exec -T postgres psql -U postgres -d edisc -Atc 'SELECT version_num FROM edisc.alembic_version')"
echo "   Temporal UI: http://localhost:28080   MinIO console: http://localhost:29001"

uv run --quiet uvicorn edisc_api.main:app --port "$API_PORT" >"$OUT/logs/api.log" 2>&1 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT
echo "   API pid $API_PID on :$API_PORT"

uv run --quiet python scripts/demo.py --out "$OUT" --api "http://127.0.0.1:$API_PORT"

bold "9. edisc-verify: the package checked offline, without database or object store"
set +e
uv run --quiet edisc-verify "$OUT/package"
status=$?
set -e
[ "$status" -eq 0 ] || { echo "expected VERIFIED" >&2; exit 1; }

bold "10. Flip one byte of one evidence object in a copy of the package"
cp -R "$OUT/package" "$OUT/package-tampered"
victim=$(find "$OUT/package-tampered/objects" -type f | sort | head -1)
uv run --quiet python - "$victim" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
data = bytearray(p.read_bytes())
data[len(data) // 2] ^= 0x01
p.write_bytes(bytes(data))
print(f"   flipped bit 0 of byte {len(data) // 2} in objects/{p.name[:16]}...")
PY
set +e
uv run --quiet edisc-verify "$OUT/package-tampered" | head -6
status=${PIPESTATUS[0]}
set -e
[ "$status" -eq 1 ] || { echo "expected FAILED (exit 1), got $status" >&2; exit 1; }

kill $API_PID 2>/dev/null || true
bold "Done in $(( $(date +%s) - T0 )) s"
echo "   outputs: $OUT/ (summary.json, package/, rsmf/, *.eml, logs/)"
echo "   the stack stays up for questions; scripts/demo.sh down removes it"
