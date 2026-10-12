# Demo: defensible Slack collection, end to end

One command. It runs against a disposable stack of its own (compose project `edisc-demo`, own
ports) and never touches the dev or test stacks. It goes from an uploaded Slack export to a
collection, an RSMF production, a collection report (JSON, HTML, PDF), a preview, the three packages
an expert would receive, and `edisc-verify` recomputing the report offline, with two tampers that
fail.

```
scripts/demo.sh          # clean start, the whole story, leaves the stack up for questions
scripts/demo.sh down     # destroy the demo stack (all volumes), the report worker and demo-output/
```

Needs Docker, `uv sync --all-packages`, and 10 GB free disk (`MIN_FREE_GB` to change). The report
PDF is made only inside the pinned linux/amd64 report image. The demo builds it
(`deploy/report-image/Dockerfile`, target `report`, tag `edisc-report:demo`, ~1 GB). The first
build takes several minutes (base image, Debian snapshot, fonts); after that it is cached. On Apple
silicon the image runs under amd64 emulation.
Temporal UI: http://localhost:28080. MinIO console: http://localhost:29001 (credentials in `.env.demo`).

## What happens, and what to say

| Step | What runs | What it proves |
|---|---|---|
| 0 | Previous demo stack, report worker and output removed | Every run starts from nothing |
| 1 | Postgres (FORCE RLS), Redis, MinIO (Object Lock), Temporal; migrations; a real API process (uvicorn) | The production topology, locally |
| 1b | The report image built; its PDF toolchain id measured inside it; a report worker **container** started from it (3 GiB, 1 CPU, PDF concurrency 1); the API creates reports for that toolchain | PDF bytes are promised only inside a pinned image, named by the toolchain id in the report's identity |
| 2 | A tenant onboarded with the dev IdP, its admin, a matter (through the API) | Tenant from the Host header + token; nothing tenant-scoped in request bodies |
| 3 | A synthetic Slack export (8 conversations x 5 days, 1,600 messages) uploaded in parts with Content-Digest; the worker hashes, locks (WORM, COMPLIANCE) and validates it | Evidence is hashed while streamed and locked before anything reads it |
| 4 | A collection job over the export. The worker process is **SIGKILLed** about a third of the way through, and a fresh worker resumes | Resumability: the database checkpoint, not memory, is the truth |
| 5 | Reconciliation per conversation-day; messages collected vs messages in the export; duplicate versions | Exactly once after a crash: 1,600 = 1,600, duplicates 0 |
| 5 (!) | File links in the export cannot be fetched offline: each is a **recorded gap with its reason**, so the job ends `completed_with_gaps` | No silent data loss: a gap is never reported as `completed` |
| 6 | Custody chain verified in the database: hash chain, Merkle roots per batch, WORM anchors | Tamper evidence for every action |
| 7 | RSMF render through the API (`RenderWorkflow`, its own custody stream): every archive entry re-read from the locked export by pinned version and verified first; 40 `.rsmf` files stored as locked productions, downloaded and hash-checked | Productions are reproducible and traceable; unfetched files are visible placeholders |
| 8 | Render package downloaded (`GET /v1/renders/{id}/package`) | Audited and anchored before the first byte; two downloads are byte-identical |
| 9 | Collection report requested with a reason (`POST /v1/jobs/{id}/reports`): `report.json`, `units.jsonl`, `observations.jsonl`, `renders.jsonl`, `report.html`, `report.pdf` (PDF/A-2u, rendered in the report container from the STORED HTML); the banner printed; the report package downloaded | The report states facts from the verified job chain first. It says NOT COMPLETE in words on every page because the job has gaps |
| 10 | Preview of one conversation-day (`GET /v1/jobs/{id}/preview`) saved as `preview.html` | Reviewers read a day without a production; the page is audited and anchored before the first byte; strict CSP, no remote fetch |
| 11 | Job custody package downloaded (`GET /v1/jobs/{id}/package`, export zip embedded) | What opposing counsel would receive |
| 12 | `edisc-verify` on the job package, the render package (`--job-package`), the report package (`--job-package`) | **VERIFIED** three times with no database and no object store; the report's `units.jsonl` and chain-derived sections **recomputed from the job chain, identical**; the clean verdict from the chain alone (NOT clean here, and the report agrees) |
| 13 | Tamper 1: one unit's collected count changed in a copy of the report package | **FAILED**: `outputs/units.jsonl` no longer matches the sealed record |
| 14 | Tamper 2: one bit flipped in one evidence object in a copy of the job package | **FAILED**, naming the object and its recorded hash |

Open `demo-output/report/report.pdf` (the banner is on every page), `demo-output/report/report.html`,
`demo-output/preview.html` in a browser, and `demo-output/<channel>_<day>_part001of001.eml` in Mail.
The last is an RSMF file: a short text summary plus `rsmf.zip`, which holds `rsmf_manifest.json` and
the attachments, or `<file id>_<name>.UNAVAILABLE.txt` placeholders (the name from the message, the
reason inside). All 40 RSMF files are in `demo-output/rsmf/`.

`docs/VERIFYING.md` is the guide an expert would follow for step 12.

## Outputs (`demo-output/`)
- `summary.json`: ids, the kill point, counts, the custody verification, the render summary, the
  report's verdict.
- `job.zip`, `render.zip`, `report.zip`: the three packages. `job-tampered/` and `report-tampered/`
  are the copies used in steps 13 and 14.
- `report/`: every report file. `preview.html`. `rsmf/*.rsmf` and one `.eml` copy.
- `verify.out`: the last `edisc-verify` output.
- `logs/`: compose, migrations, report image build, API, `worker-1.log` (the killed worker),
  `worker-2.log`, `report-worker.log`.

## Questions to expect
- **Why `completed_with_gaps`?** The synthetic export references attachments on Slack's file host,
  which the demo cannot reach (the worker only fetches from allowlisted public hosts). Reporting that
  honestly is the point. A reachable host gives `completed_against_archive` (the integration tests
  do this with a stand-in host).
- **Why "against archive"?** An export can only prove completeness relative to itself. The ADR 0014
  caveat is shown wherever that basis applies, and such a job is never marked clean.
- **What does the verifier recompute, and what not?** Section 5 of docs/VERIFYING.md. In short: unit
  rows byte for byte, counts, exceptions, conversations, the job, scope, access, pause, actor and
  normalizer-version sections, and the clean verdict (one way). Observations, renders, the snapshot
  and divergences are checked by digest only, and the output says so.
- **Why is the PDF made in a container?** Its bytes depend on the layout engine, fonts and zlib.
  They are pinned in the image and named by the toolchain id, and the image is admitted only if its
  PDFs match the goldens byte for byte.
- **Is the RSMF validated?** Against Relativity's published JSON schema (BSD-3, vendored), plus
  structural checks. Relativity's own validator waits on the licence question.
- **What is not shown:** the live Slack connector (Phase 3), the web UI (another branch),
  report reproductions on old images (BACKLOG).

## Timing (2026-10-12, this laptop, report image cached)
Three runs from clean: 70 s, 70 s, 62 s. The first run that builds the report image adds about 1.5
minutes here (layers shared with `edisc-report-test:dev`) and several minutes on a machine without any
cached layers. Same results each time: 1,600 messages collected = 1,600 in the export, duplicates 0,
the kill at 13-15 of 40 units, 84 custody events, 40 RSMF files, a report of 6 files (`report.pdf`
48 KB, PDF toolchain `e24aecbb3dd4`, the admitted one), VERIFIED three times with the report
recomputed (41 `units.jsonl` rows) and the chain verdict NOT clean, both tampers FAILED.

## Troubleshooting
- `refusing: N GB free`: free disk, or `MIN_FREE_GB=8 scripts/demo.sh`.
- Port in use (18100, 25432, 26379, 27233, 28080, 29000): another demo is still up, so run
  `scripts/demo.sh down`.
- `report image build failed`: see `demo-output/logs/report-image.log`. Docker Hub pulls can stall
  for minutes the first time. Do not cancel a build stuck at `FROM`.
- The report waits for its PDF longer than expected: amd64 emulation is slow. See
  `demo-output/logs/report-worker.log`.
- Failures stop the script loudly. The logs are in `demo-output/logs/`.
