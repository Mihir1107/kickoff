# Mutation checks

A test that cannot fail protects nothing. Each entry in `catalog.py` breaks ONE protection with an
exact text edit and names the test that must then fail. `run.py` applies one break at a time, runs
that test, records whether it failed ("caught"), and restores the file from a copy before the next
one (never with `git checkout`, so other uncommitted work in the tree survives).

## Running

```
uv run python scripts/mutation/run.py --list                 # what is in the catalog
uv run python scripts/mutation/run.py --check                # every edit still applies (no tests run)
uv run python scripts/mutation/run.py --kind unit            # no services needed (about 2 minutes)
MIN_FREE_GB=12 make test-env-up                              # integration breaks need the test stack
uv run python scripts/mutation/run.py --kind integration     # about 10 minutes on a laptop
make test-env-down
uv run python scripts/mutation/run.py --round s21-review --only native_copy_unlocked -v
make test-report-pdf                                         # runs --kind image INSIDE the report image
```

Exit status 0 only if every selected break was caught and every edit applied. `-v` prints the tail
of each test run. Integration breaks run through `make test-integration-only` (the ephemeral
`edisc-test` stack, never the dev stack). If a run is killed hard (SIGKILL), a file may be left
broken next to a `<name>.mutation-backup` copy: the runner refuses to start until you restore it
(`mv <file>.mutation-backup <file>`).

## Keeping it current

- `tests/unit/test_mutation_catalog.py` (part of `make check`) fails when an edit no longer applies
  exactly once or a named test file is gone: update the entry in the same change as the refactor.
- Every new protection gets an entry here, in a round named after the ADR section that introduced it.
- A break that is NOT caught means a missing or weak test: strengthen the test (do not delete the
  entry). If two independent mechanisms guard one property (natives reach their matter's retention
  by `job_id` AND `render_id`), give each its own test and its own break.

## Rounds

| round | what | entries |
|---|---|---|
| `s19-part-c` | render package download, ADR 0015 §19.9 (reconstructed from its recorded list) | 20 |
| `s19-review` | first-byte anchoring, Content-Length, strict verifier, anchor divergences (§19.11-14) | 11 |
| `s20-natives` | oversized attachments as natives (§20, §21) | 45 |
| `s21-review` | placeholder name encoding, concurrent writers, the dummy connector pin (§21 review) | 8 |
| `ci-heartbeat` | slice rendering off the event loop: heartbeats never starved (CI run 37412915073, ADR 0015 §23) | 1 |
| `s24-loop` | the event-loop guard, every CPU step of an activity off the loop, the render thread writes nothing (ADR 0015 §24) | 43 |
| `m16-model` | the collection report model, loader and recorded access facts (ADR 0018, M16 step 1) | 21 |
| `m16-stream` | the report lifecycle, custody stream, stored files, retention, episodes (ADR 0018, M16 step 4) | 13 |
| `m16-html` | the report HTML builder and its sanitiser (ADR 0018 §3.2, §4; M16 step 2) | 8 |
| `m16-pre3` | marker elements, HTML-invalid code points, `conversations.jsonl` above the cap, report goldens, CI shards (ADR 0018 §21) | 14 |
| `m16-pdf` | the report PDF child and toolchain (ADR 0018 §5, §6, §22; M16 step 3) | 14 |
| `m16-pre5` | lone surrogates, the glyph set in the identity, PDF from stored HTML (tamper), image-only breaks (kind `image`, run in the report image), production install (ADR 0018 §23-§26) | 27 |
| `m16-api` | job package download, reports API, report package, `edisc-verify` recomputation (ADR 0018 §27) | 17 |
| `m16-pre6` | hostile export values, every unit fact in the chain, the strict job verifier reading zips in place (ADR 0018 §28) | 24 |
| `m16-preview` | the preview route, renderer, sanitiser, link allow-list, day-bounded loader (ADR 0018 §29) | 11 |
| `m16-pre7` | code-point order under any database collation, the clean verdict from the chain alone, normalizer versions in the chain (ADR 0018 §30) | 15 |
| `m16-export` | the export a job read, in its report: recorded, stated, recomputed, in the HTML; export ingestion under a tailored collation (ADR 0018 §32) | 8 |
| `m17-d11` | no error path echoes a request value: sanitised 422s, fixed 500s, value-free log lines, uvicorn's access log off, hidden statement parameters, redacted worker texts, route refusals (ADR 0016 A8, M17 step 0) | 17 |
| `m17-sessions` | browser sessions: fixation, expiries, revocation, cookie flags, one credential, CSRF, fetch metadata, single-use state, nonce, `return_to`, re-authentication, the bounce page in real browsers, recent sign-in, guard triggers (ADR 0016; M17 step 2) | 35 |
| `m17-logs` | every test asserting on log output reads the production pipeline and proves its lines arrived (the sweep before M17 step 2) | 7 |
| `s24-pool-exhaustion` | a checkout beyond a fixed pool (database or the API's Redis) waits a bounded time, then 503 `db_pool_exhausted` / `service_busy` (API) or a transient activity error (workers), never a hang | 8 |
| `m17-client-auth` | `private_key_jwt` at the sign-in IdP: assertion claims, fresh `jti`, lifetime, sent at all, public client refused outside local/test/ci, discovery check, published keys, key work off the loop, each check of the test IdP (M17 step 3b) | 27 |
| `m17-groups` | the IdP's groups claim is never "no groups": overage and an absent/malformed claim (roles depending on groups) refuse sign-in and bearer, audited, alerted once | 6 |
| `m17-openapi` | the published contract: drift check, operationIds, every compatibility rule (oasdiff ERR, D9 downgrade, our rules), the allow-list, the pinned oasdiff checksum (M17 step 4) | 20 |
| `m17-error-codes` | every route declares its codes from one registry; the test-time check (undeclared code, wrong status, no code); framework refusals coded; the compat rule on the real document | 8 |
| `m17-stream` | the job stream: re-checks (session, principal, permission), passive authentication, Last-Event-ID without misses or repeats, the bounded queue and slow_consumer, sealed and lifetime ends, the per-principal cap, import and SQL boundaries, no evidence content, the SSE compat rule, the signing key loaded off the loop, no subscriber or connection left by a disconnect (M17 step 5) | 16 |
| `m17-close` | closing M17: the production-readiness gate's modes (warn on main, enforce on tags and the production image), the report's pause endings (stated, recomputed, in the HTML, the divergence), the SSE `connection` event (polled, in the digest, never repeated, published, status only), the directory snapshots (never partial, checkpoint, real SIGKILLs, generations, D12, budget, failures, prefix, D14, exports, off the loop, lone surrogates, the sweep, bounded work per page at any depth) | 43 |

600 entries: 222 unit, 374 integration, 4 image.

The `s19-*` rounds were run by hand when they were built and only their categories were written
down; the entries here re-create them against the current code.
