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

The `s19-*` rounds were run by hand when they were built and only their categories were written
down; the entries here re-create them against the current code.
