# Demo: defensible Slack collection, end to end

One command, about 30 seconds on a warm laptop (plus image pulls the first time). It runs against a
disposable stack of its own (compose project `edisc-demo`, own ports). The dev and test stacks are
never touched.

```
scripts/demo.sh          # clean start, the whole story, leaves the stack up for questions
scripts/demo.sh down     # destroy the demo stack (all volumes) and demo-output/
```

Needs Docker, `uv sync --all-packages`, and 10 GB free disk (`MIN_FREE_GB` to change).
Temporal UI: http://localhost:28080. MinIO console: http://localhost:29001 (credentials in `.env.demo`).

## What happens, and what to say

| Step | What runs | What it proves |
|---|---|---|
| 0 | Previous demo stack and output removed | Every run starts from nothing |
| 1 | Postgres (FORCE RLS), Redis, MinIO (Object Lock), Temporal; migrations; a real API process (uvicorn) | The production topology, locally |
| 2 | A tenant onboarded with the dev IdP, its admin, a matter (through the API) | Tenant from the Host header + token; nothing tenant-scoped in request bodies |
| 3 | A synthetic Slack export (8 conversations x 5 days, 1,600 messages) uploaded in parts with Content-Digest; the worker hashes, locks (WORM, COMPLIANCE) and validates it | Evidence is hashed while streamed and locked before anything reads it |
| 4 | A collection job over the export. The worker process is **SIGKILLed** about a third of the way through (`14/40 units`), and a fresh worker resumes | Resumability: the database checkpoint, not memory, is the truth |
| 5 | Reconciliation per conversation-day; messages collected vs messages in the export; duplicate versions | Exactly once after a crash: 1,600 = 1,600, duplicates 0 |
| 5 (!) | 34 file links the export points at cannot be fetched offline: each is a **recorded gap with its reason**, so the job ends `completed_with_gaps` | No silent data loss: a gap is never reported as `completed` |
| 6 | Custody chain verified in the database: hash chain, Merkle roots per batch, WORM anchors | Tamper evidence for every action |
| 7 | The offline custody package (events, items, evidence records, the export zip itself) | What opposing counsel would receive |
| 8 | RSMF render of the sealed job (M15 step 3): every archive entry re-read from the locked export by pinned version and verified first; 40 `.rsmf` files written as locked production evidence | Productions are reproducible and traceable: 1,600 items in = 1,600 events out, 74 thread-context events; the unfetched files are visible placeholders |
| 9 | `edisc-verify demo-output/package` | **VERIFIED** with no database and no object store |
| 10 | One bit flipped in the export inside a copy of the package, then `edisc-verify` again | **FAILED**, naming the object and both hashes |

Open `demo-output/<channel>_<day>_part001of001.eml` in Mail. It is the RSMF file (an RSMF is an
RFC 5322 message): a short text summary plus `rsmf.zip`, which holds `rsmf_manifest.json` and the
attachments or `<file id>_<name>.UNAVAILABLE.txt` placeholders (the name from the message, the reason inside). All 40 files are in `demo-output/rsmf/`.

## Outputs (`demo-output/`)
- `summary.json`: ids, the kill point, counts, the custody report, the render reconciliation.
- `package/` and `package-tampered/`: the custody package before and after the flipped bit.
- `rsmf/*.rsmf` and one `.eml` copy.
- `logs/`: compose, migrations, API, `worker-1.log` (the killed worker), `worker-2.log`.

## Questions to expect
- **Why `completed_with_gaps`?** The synthetic export references attachments on Slack's file host,
  which the demo cannot reach (the worker only fetches from allowlisted public hosts). Reporting that
  honestly is the point. A reachable host gives `completed_against_archive` (the integration tests
  do this with a stand-in host).
- **Why "against archive"?** An export can only prove completeness relative to itself. The ADR 0014
  caveat is shown wherever that basis applies.
- **Is the RSMF validated?** Against Relativity's published JSON schema (BSD-3, vendored), plus
  structural checks. Relativity's own validator waits on the licence question.
- **What is not shown yet:** the render workflow, its custody stream and API (M15 step 4; the demo
  calls the step 3 renderer directly), and the live Slack connector (Phase 3).

## Timing (2026-10-04, this laptop, images cached)
Three runs from clean (the last on renderer 1.1.0): 32 s, 31 s, 31 s, same results each time (1,600 messages, kill at
14/40 units, 84 custody events, 40 RSMF files, VERIFIED then FAILED).

## Troubleshooting
- `refusing: N GB free`: free disk, or `MIN_FREE_GB=8 scripts/demo.sh`.
- Port in use (18100, 25432, 26379, 27233, 28080, 29000): another demo is still up, so run
  `scripts/demo.sh down`.
- Failures stop the script loudly. The logs are in `demo-output/logs/`.
