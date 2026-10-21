#!/usr/bin/env bash
# Mentor demo (docs/DEMO.md). Usage:
#   scripts/demo.sh        clean start: stack up, seed, export upload, job with a SIGKILL, reconciliation,
#                          custody verify, RSMF render, render package, collection report (JSON, HTML,
#                          PDF in the report image), preview, job package, edisc-verify (job, render,
#                          report recomputed from the job chain), two tampers that FAIL
#   scripts/demo.sh down   destroy the demo stack (all its volumes) and demo-output/
# Uses its own compose project (edisc-demo) and ports; never the dev or test stacks. Both forms first
# remove EVERY container, volume and network of project edisc-demo, including a stack another
# checkout of this repository left, and refuse to go on if any of it remains.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=edisc-demo
ENV_FILE=.env.demo
OUT=demo-output
API_PORT=18100
MIN_FREE_GB=${MIN_FREE_GB:-10}
REPORT_IMAGE=${REPORT_IMAGE:-edisc-report:demo}
REPORT_WORKER=edisc-demo-report-worker
COMPOSE=(docker compose -p "$PROJECT" -f infra/docker-compose.yml --env-file "$ENV_FILE")
export EDISC_ENV_FILE=$ENV_FILE EDISC_COMPOSE_PROJECT=$PROJECT

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
# Everything of compose project $PROJECT, whichever checkout started it: compose labels its containers,
# volumes and networks with the project name, and another checkout's demo stack has the same name.
# Found by label, not through `compose down`, which needs this checkout's .env.demo (a fresh clone has
# none, and then reused the old stack: its database, evidence and Temporal history).
project() { docker "$@" --filter "label=com.docker.compose.project=$PROJECT"; }
teardown() {
  docker rm -f "$REPORT_WORKER" >/dev/null 2>&1 || true
  local containers volumes networks others
  containers=$(project ps -aq)
  if [ -n "$containers" ]; then
    # shellcheck disable=SC2086  # ids, one per word
    others=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
      $containers | sort -u | grep -vxF "$(pwd -P)/infra" || true)
    [ -z "$others" ] || echo "   removing the $PROJECT stack another checkout left: $others"
    docker rm -f -v $containers >/dev/null || true  # shellcheck disable=SC2086
  fi
  volumes=$(project volume ls -q)
  [ -z "$volumes" ] || docker volume rm -f $volumes >/dev/null || true  # shellcheck disable=SC2086
  networks=$(project network ls -q)
  [ -z "$networks" ] || docker network rm $networks >/dev/null || true  # shellcheck disable=SC2086
  # whatever failed above is caught here, loudly: never start on top of a stack that is still there
  if [ -n "$(project ps -aq)$(project volume ls -q)$(project network ls -q)" ]; then
    echo "refusing: the $PROJECT stack (containers, volumes or networks) could not be removed." >&2
    echo "  See what is left: docker ps -a, docker volume ls, docker network ls" >&2
    echo "    --filter label=com.docker.compose.project=$PROJECT; remove it, then run scripts/demo.sh again." >&2
    exit 1
  fi
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

bold "1b. Report image (linux/amd64: the pinned PDF toolchain) and a report worker container"
docker buildx build --platform linux/amd64 -f deploy/report-image/Dockerfile --target runtime \
  -t "$REPORT_IMAGE" --load . >"$OUT/logs/report-image.log" 2>&1 \
  || { echo "report image build failed: $OUT/logs/report-image.log" >&2; exit 1; }
TOOLCHAIN=$(docker run --rm --platform linux/amd64 --entrypoint /opt/edisc/venv/bin/python "$REPORT_IMAGE" \
  -m edisc_worker.versions | uv run --quiet python -c 'import json,sys; print(json.load(sys.stdin)["toolchain_id"])')
echo "   image $REPORT_IMAGE, PDF toolchain ${TOOLCHAIN:0:12}"
# the worker container reaches the demo stack through the host (Docker Desktop) or host networking
net=(); host=host.docker.internal
if [ "$(uname)" = "Linux" ]; then net=(--network host); host=localhost; fi
envs=()
while IFS= read -r line; do
  case "$line" in EDISC_*=*|TEMPORAL_*=*) ;; *) continue ;; esac
  key=${line%%=*}; value=${line#*=}; value=${value#\'}; value=${value%\'}
  value=${value//localhost/$host}; value=${value//127.0.0.1/$host}
  envs+=(-e "$key=$value")
done <"$ENV_FILE"
docker run -d --name "$REPORT_WORKER" --platform linux/amd64 ${net[@]+"${net[@]}"} --memory 3g --cpus 1 \
  "${envs[@]}" "$REPORT_IMAGE" --reports --source dummy --queue collect-unused-demo >/dev/null
echo "   report worker container $REPORT_WORKER (PDF concurrency 1, 3 GiB, CPU 1)"

EDISC_REPORT_TOOLCHAIN_ID=$TOOLCHAIN uv run --quiet uvicorn edisc_api.main:app --port "$API_PORT" \
  >"$OUT/logs/api.log" 2>&1 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true; docker logs "$REPORT_WORKER" >"$OUT/logs/report-worker.log" 2>&1 || true' EXIT
echo "   API pid $API_PID on :$API_PORT (new reports: toolchain ${TOOLCHAIN:0:12})"

uv run --quiet python scripts/demo.py --out "$OUT" --api "http://127.0.0.1:$API_PORT"

verify() {  # verify EXPECTED_EXIT ARGS...: run edisc-verify, show its output, insist on its exit code
  local want=$1; shift
  set +e
  uv run --quiet edisc-verify "$@" >"$OUT/verify.out" 2>&1
  local got=$?
  set -e
  sed 's/^/   /' "$OUT/verify.out"
  [ "$got" -eq "$want" ] || { echo "expected exit $want from edisc-verify $*, got $got" >&2; exit 1; }
}

bold "12. edisc-verify, offline (no database, no object store): the job, the render, the report"
verify 0 "$OUT/job.zip"
verify 0 "$OUT/render.zip" --job-package "$OUT/job.zip"
verify 0 "$OUT/report.zip" --job-package "$OUT/job.zip"
grep -q "RECOMPUTED from the job chain, identical" "$OUT/verify.out" \
  || { echo "expected the report to be recomputed from the job chain" >&2; exit 1; }

bold "13. Tamper 1: one unit row of the report changed (collected count) in a copy of the package"
mkdir -p "$OUT/report-tampered"
uv run --quiet python - "$OUT/report.zip" "$OUT/report-tampered" <<'PY'
import re, sys, zipfile
from pathlib import Path
dest = Path(sys.argv[2])
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(dest)
units = dest / "outputs" / "units.jsonl"
lines = units.read_bytes().splitlines(keepends=True)
i = next(i for i, l in enumerate(lines) if b'"collected":' in l and b'"kind":"conversation_day"' in l)
lines[i] = re.sub(rb'"collected":(\d+)', lambda m: b'"collected":%d' % (int(m[1]) + 1), lines[i], count=1)
units.write_bytes(b"".join(lines))
print(f"   outputs/units.jsonl row {i + 1}: collected + 1")
PY
verify 1 "$OUT/report-tampered" --job-package "$OUT/job.zip"

bold "14. Tamper 2: one bit of one evidence object flipped in a copy of the job package"
mkdir -p "$OUT/job-tampered"
uv run --quiet python - "$OUT/job.zip" "$OUT/job-tampered" <<'PY'
import sys, zipfile
from pathlib import Path
dest = Path(sys.argv[2])
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(dest)
p = sorted((dest / "objects").iterdir())[0]
data = bytearray(p.read_bytes())
data[len(data) // 2] ^= 0x01
p.write_bytes(bytes(data))
print(f"   flipped bit 0 of byte {len(data) // 2} in objects/{p.name[:16]}...")
PY
verify 1 "$OUT/job-tampered"

kill $API_PID 2>/dev/null || true
bold "Done in $(( $(date +%s) - T0 )) s"
echo "   outputs: $OUT/ (summary.json, job.zip, render.zip, report.zip, report/, preview.html, rsmf/, *.eml, logs/)"
echo "   the stack stays up for questions; scripts/demo.sh down removes it"
