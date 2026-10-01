# ADR 0012: Temporal workflow design: fan-out, continue-as-new, errors, cancellation, versioning

Status: **Proposed** (2026-10-01), for review before M12 is implemented. Builds on ADR 0001 and
ADR 0006; the activity bodies already exist (`edisc_worker.pipeline`).

## 1. Shape

```
CollectionJobWorkflow  id = "{job_id}"                    task queue collect-{source}
  ├─ activity start_job / enumerate_units                  (idempotent; units written to the DB)
  ├─ loop: start ≤ N CollectUnitWorkflow children, wait for signals / periodic DB reconcile
  │        continue-as-new when suggested (state = in-flight unit keys, flags)
  └─ activity finalize_job | finalize_cancelled            (recover evidence, status, custody, seal)

CollectUnitWorkflow    id = "{job_id}/{unit_key}"
  ├─ loop: activity collect_pages(max_pages)               (heartbeat details = cursor)
  │        continue-as-new after K iterations
  ├─ activity finalize_unit                                (reconcile; absence detection if clean)
  └─ signal parent: unit_finished(unit_key, outcome)
```

Inputs and outputs are only ids, unit keys, small counters and flags. No raw data, tokens or item lists
go into history.

## 2. Fan-out and continue-as-new (requirement 4)

**The database is the source of truth; the parent never awaits child handles.**
- Children are started with `parent_close_policy=ABANDON`. When the parent continues-as-new, or even
  fails, running children keep running.
- **IDs are deterministic:** the parent is `{job_id}`, and each child is `{job_id}/{unit_key}`.
- **Re-attach after continue-as-new:**
  - The new run receives the in-flight unit keys in its input. It does not need their handles.
  - To (re)start a unit, it calls `start_child_workflow` with the deterministic ID. If a child with
    that ID is already running, Temporal refuses the start (already started). The parent treats that
    unit as attached and in flight.
  - Completion reaches the parent by **signal to the parent workflow ID**. Signals address the
    workflow ID, so they land on whichever run is current.
  - As a backstop, every few minutes, and immediately after each continue-as-new, the parent runs an
    activity that reads unit statuses from the DB. That catches any signal that raced a
    continue-as-new. Correctness never depends on a signal being delivered.
- **Bounded concurrency:** the parent keeps at most `N` units in flight (default 8, configurable).
  Before continuing-as-new it waits for `all_handlers_finished()`, so no signal handler is lost
  mid-flight.
- **ID reuse:**
  - Parent: `id_reuse_policy=REJECT_DUPLICATE` and `id_conflict_policy=FAIL`. Starting the same job
    twice fails with "already started", even after it finished; the API maps that to "job already
    exists". The DB `start_job` is idempotent as well.
  - Children: `ALLOW_DUPLICATE`. The server already refuses a duplicate while one is **running**.
    A **closed** child may be started again, which is needed to resume a unit after
    re-authorization or after a unit-level retry. Whether a unit needs a (new) child is decided only
    by its DB status.

Why not await child handles: a handle belongs to the run that started the child. After
continue-as-new the new run cannot await it, and "re-attaching" would mean restarting it.
DB + deterministic IDs + signals is the documented pattern for long fan-outs that outlive a run.

## 3. Error classification (requirement 2)

Activities translate exceptions into Temporal `ApplicationError` types. Workflows never see raw
exceptions.

| Class | Examples | Activity retry | Outcome |
|---|---|---|---|
| **Integrity** (non-retryable) | `EvidenceIntegrityError`, chain/Merkle verification failure, `NormalizationError`, invalid cursor, `MultiScopeNotSupportedError` | none | Child runs `fail_unit`: the unit is `failed` with the error, plus a custody `unit_failed` lifecycle event. The job ends `failed`. Loud, never retried away |
| **Auth** (non-retryable) | `AuthenticationError` (new in connectors/base: token revoked or invalid, `invalid_auth`, `token_revoked`, `invalid_grant`) | none | Child ends `paused`. Parent: job `paused_awaiting_reauth` (new status), custody `job_paused` event, no new units. Waits for a `reauthorized` signal (sent by the API after reconnecting), then resumes the paused units. No infinite retries |
| **Transient** (retryable) | `TimeoutError`, `ConnectionError`, source 5xx, `is_retryable_db_error`, `ContentLockTimeoutError`, `EvidenceCopyTimeoutError`, S3 5xx/throttling | exponential 1 s → 60 s, at most 25 attempts | When exhausted: treated as integrity, so the unit is `failed` loudly |
| **Throttle** | source 429 / Retry-After | handled inside the limiter (shared pause), never an activity failure | |
| **Unclassified** | anything else | at most 3 attempts | then `failed` with the exception type in the unit's `last_error` |

**File unavailable (bounded policy).**
- *Permanent* reasons (deleted, external/hidden, permission) are recorded at once.
- *Transient* reasons (expired URL) are retried inside the batch up to `EDISC_FILE_RETRY_ATTEMPTS`
  (default 3) with backoff, then recorded. The unit is a gap.
- Later jobs retry naturally. `file_became_available` records success.

## 4. Heartbeats (requirement 3)

- `collect_pages` heartbeats after every batch with details `{unit_key, cursor, pages}`. The details
  are informational: on retry the activity resumes from the **DB checkpoint** (ADR 0001).
- Waits inside the rate limiter (throttle, shared pause, Redis down) call the existing `on_wait`
  callback, which heartbeats. Connectors call `call_with_limits` without knowing about Temporal, so
  the callback reaches them through a context variable that the activity sets.
- `heartbeat_timeout` is 60 s, and `start_to_close` is 30 min with `max_pages` bounding the work.
  A slow but alive activity always heartbeats in time; a dead worker is detected within 60 s.

## 5. Cancellation (requirement 5)

Cancellation is cooperative and stops at a batch boundary, never mid-transaction.
1. A user cancel (API: `cancel_job` signal, or a Temporal workflow cancel, both handled the same way)
   makes the parent run `request_cancel`. That sets `collection_jobs.cancel_requested_at` and
   appends a custody `cancel_requested` event.
2. `collect_pages` checks the flag **before each batch**, outside any transaction, and returns
   `cancelled`. A batch already in its transaction always commits normally.
3. The parent starts no new units. It waits until in-flight children have reported, then runs
   `finalize_cancelled`: recover pending evidence, unit statuses stay as they are, custody
   `job_cancelled` lifecycle event, seal the chain, job status `cancelled`.

## 6. Determinism and versioning (requirement 1)

- Workflows run in the Temporal sandbox. They use only `workflow.now()` and no I/O, randomness,
  wall clock or environment. Every id is generated in activities.
- **Replay tests in CI** (`tests/unit/temporal`, no server): recorded histories of both workflows,
  saved by the integration tests to `tests/golden/temporal/*.json`, are replayed with
  `temporalio.worker.Replayer` against the current code. Covered histories: a full job, a
  continue-as-new, a crash/retry, a cancel, and an auth pause.
- **Policy for changing workflow code while multi-day jobs run:**
  1. Every change to workflow code that alters the command sequence goes behind
     `workflow.patched("<change-id>")`.
  2. The new history is recorded. The old histories stay in the replay suite for as long as a run
     started on the old code could exist (the longest job plus the continue-as-new horizon; 30 days
     by default).
  3. After that, `workflow.deprecate_patch`, and later remove the old branch with its golden history.
  - Activity code may change freely within its contract.
  - Worker build IDs / Worker Deployments are the route for large incompatible rewrites (backlog).
  - Tested: one patched change replays an in-flight history recorded before the change. The same
    change without the patch fails replay with a nondeterminism error (negative control).

## 7. Sweepers (requirement 6)

Each sweeper is a Temporal Schedule with `overlap=SKIP`, running a one-activity workflow on the
`maintenance` task queue:

| Schedule | Every | Activity |
|---|---|---|
| `sweep-anchors` | 5 min | `sweep_anchors` (M5.1) |
| `reconcile-token-refreshes` | 10 min | `reconcile_token_refreshes` (M7.1) |
| `sweep-stale-uploads` | 1 h | For each (tenant, job) with `pending` evidence older than copy timeout + 1 h, `recover_job_evidence`. Listed through a new sweeper-login definer function, ids only |

## 8. Acceptance (requirement 7)

- **CI test:** 50,000 messages (10 conversations × 10 days × 500), page size 200.
  - 3 worker **processes**, with 8 units in flight.
  - The test SIGKILLs a random worker at random times, 3 to 5 times, and also kills **all** workers
    at once and restarts them.
  - It asserts: results equal the oracle exactly, zero duplicates, a valid sealed chain, no pending
    evidence, and every unit reconciled.
- **Manual run:** 1,000,000 messages, the same script with `--messages 1000000`. Results go in
  `docs/runs/`.
- **Performance:** today's per-item SQL takes about 3 statements per item, too slow for 50k in a CI
  budget. M12 batches `persist` and the job-link inserts into multi-row statements, and measures the
  result.

## Consequences
- + Jobs survive any number of continue-as-new, worker crashes and deploys. Children are independent
  and restartable by ID.
- + Correctness rests on the DB checkpoint and idempotent activities, not on signal delivery or
  history.
- − The parent polls the DB periodically. That is cheap: one activity per few minutes per job.
- − `REJECT_DUPLICATE` means a job id can never be reused. That is intended: job ids are UUIDv7 and
  a retried API call must not start a second job.
