# ADR 0012: Temporal workflow design: fan-out, continue-as-new, errors, cancellation, versioning

Status: **Accepted** (2026-10-01) with review changes (sections marked **R1–R6**). Builds on ADR 0001 and
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

## 2a. Sealed jobs are closed (R1)
- Before **every** batch, outside any transaction, children check the job state: cancel requested,
  failing, paused, terminal or sealed. A job that is not running produces no more batches.
- A **DB trigger** on `items`, `job_items` and `custody_events` rejects any insert for a job whose
  status is terminal or whose `sealed_at` is set. The trigger takes `FOR SHARE` on the job row, so a
  batch transaction racing the finalize transaction either commits before the job's terminal status
  or is rejected and rolled back. Nothing can land after `job_finished` / the seal.
- Finalize order: recover evidence (custody events allowed) → `job_finished` event and terminal
  status in one transaction → seal anchor → `sealed_at`.
- Tested: a job-scoped integrity failure in one unit fails the job while other children are
  mid-run. Zero events land after the seal, and `verify_chain` passes.

## 3. Error classification (requirement 2)

Activities translate exceptions into Temporal `ApplicationError` types. Workflows never see raw
exceptions.

| Class | Examples | Activity retry | Outcome |
|---|---|---|---|
| **Integrity, unit-scoped** (non-retryable) (R4) | `NormalizationError`, invalid cursor, a single `EvidenceIntegrityError` | none | Child runs `fail_unit`: the unit is `failed` with the error and a custody `unit_failed` event. **Other units finish.** The job ends **`completed_with_failed_units`**, which is never shown as clean. Failed units are re-runnable after a fix as a **rerun job** (`rerun_of`, explicit unit list); the sealed original stays closed |
| **Integrity, job-scoped** (non-retryable) (R4) | custody chain/Merkle verification failure, WORM anchor conflict/mismatch | none | The parent fails the **job**. Every unit stops at its next batch boundary, then custody `job_failed`, seal, status `failed` |
| **Auth** (non-retryable) (R6) | `AuthenticationError` (new in connectors/base: token revoked or invalid, `invalid_auth`, `token_revoked`, `invalid_grant`) | none | The connection becomes `reauth_required`, and **every running job on that connection** becomes `paused_awaiting_reauth`. Each gets a custody `job_paused` event and a `job_pauses` row, and one **alert record** is created per connection. Children stop at their next batch boundary, and parents start no units. When re-authorized (API, M13): pauses close, jobs resume (signal plus DB poll as backstop). The paused duration is in the job status and the report. No infinite retries |
| **Transient** (retryable) (R3) | `TimeoutError`, `ConnectionError`, source 5xx, `is_retryable_db_error`, `ContentLockTimeoutError`, `EvidenceCopyTimeoutError`, S3 5xx/throttling | exponential 1 s → 60 s, at most 25 attempts | When exhausted: the unit goes to **`retry_later`** with a cool-down (`EDISC_UNIT_RETRY_COOLDOWN_SECONDS`, default 15 min). The parent re-schedules it while other units continue. Only after `EDISC_UNIT_RETRY_HORIZON_SECONDS` (default 24 h) since its first failure is it `failed`, with the last error recorded |
| **Throttle** | source 429 / Retry-After | handled inside the limiter (shared pause), never an activity failure | |
| **Unclassified** | anything else | at most 3 attempts | then `failed` with the exception type in the unit's `last_error` |

**File unavailable (bounded policy).**
- *Permanent* reasons (deleted, external/hidden, permission) are recorded at once.
- *Transient* reasons (expired URL) are retried inside the batch up to `EDISC_FILE_RETRY_ATTEMPTS`
  (default 3) with backoff, then recorded. The unit is a gap.
- Later jobs retry naturally. `file_became_available` records success.

## 4. Heartbeats and activity duration (requirement 3, R2)

**Choice: time-boxed activities.** A unit can take hours: at Slack's non-Marketplace rate of
15 messages/min, 500 messages take about 33 minutes. So `collect_pages` processes batches only until
`max_pages` or `EDISC_ACTIVITY_TIME_BOX_SECONDS` (default 600 s), whichever comes first, then returns.
The child workflow loops, and each call resumes from the DB checkpoint. `start_to_close` = 30 min
(time box plus one batch plus margin). `heartbeat_timeout` = 60 s.

The limiter's `on_wait` callback heartbeats, and it also checks:
- the time box;
- the job's cancel, fail and pause state, read from the DB and cached for at most 5 s.

If either says stop, the activity ends cleanly *before* the request. Waits are never inside a
transaction. So a long Retry-After or a Redis outage can neither exceed `start_to_close` nor delay a
cancel.


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
  2. The new history is recorded. **The old histories stay in the replay suite until Temporal
     visibility shows no open workflow that started before the patch shipped** (R5; no fixed time
     rule). `scripts/temporal_patch_check.py` runs that query:
     `WorkflowType=… AND ExecutionStatus="Running" AND StartTime < <patch deploy time>`.
     Continue-as-new runs count as started at their own start time, so the query covers chains.
  3. Only when that query returns nothing: `workflow.deprecate_patch`, then remove the old branch with
     its golden histories.
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

## Implementation notes (M12 commit 1)
- Code: `edisc_worker.contracts` (sandbox-safe values), `errors` (classification), `activities`,
  `workflows`, `__main__` (one worker per source, token-refresh reconcile at startup).
- In-flight backstop: besides `job_overview` (DB), the parent runs `closed_children` every poll timeout
  and right after continue-as-new. It asks Temporal (describe by deterministic id) which in-flight
  children are closed, so a child that ended without signalling (terminated, failed) never blocks the
  job. Re-starting a unit is always safe: `collect_pages` resumes from the DB checkpoint.
- Control activities (`job_overview`, `fail_unit`, `defer_unit`, `request_stop`, `pause_for_reauth`,
  `finalize_job`, ...) retry without an attempt limit (backoff capped at 60 s): a long DB/S3 outage must
  not orphan a job. Non-retryable classes still fail at once. `collect_pages`, `finalize_unit` and
  `enumerate_units` use the 25-attempt budget.
- Unclassified errors: the activity marks its own error non-retryable at attempt
  `EDISC_UNCLASSIFIED_MAX_ATTEMPTS` (3); the unit fails with the exception type in `last_error`.
- Limiter waits are slept in chunks of at most `wait_chunk_seconds` (10 s), so `on_wait` (heartbeat,
  time box, job state) runs at least that often during a long Retry-After.
- Transient file refusals (`expired_url`) are retried in the batch `EDISC_FILE_RETRY_ATTEMPTS` times with
  backoff `EDISC_FILE_RETRY_BACKOFF_SECONDS`; permanent refusals are recorded at once.
- Tuning travels in the workflow input (`RunConfig.from_settings`), never read inside workflows.

## Implementation notes (M12 commit 2: schedules)
- `edisc_worker.maintenance`: `ensure_schedules` creates or updates `sweep-anchors` (5 min),
  `reconcile-token-refreshes` (10 min) and `sweep-stale-uploads` (1 h), each starting
  `MaintenanceWorkflow(<activity>)` on the `maintenance` queue with overlap SKIP. `make worker` runs
  `python -m edisc_worker --maintenance`, which also ensures the schedules at start (idempotent).
- Stale uploads: migration 0013 adds `stale_pending_evidence(min_age, limit, tenant)` (SECURITY
  DEFINER, owned and executable only by the sweeper login, ids only; it sees only pending evidence rows
  and unfinished jobs). `edisc_custody.recovery.sweep_stale_uploads` recovers each job as the app role
  with an `evidence_recovered` custody event; default age = copy timeout + 1 h. Finished jobs are
  skipped (finalize already recovered them; a sealed job is closed).

## Implementation notes (M12 commit 3: performance and acceptance)
- `persist` and the job-link insert are now a constant number of statements per batch (advisory locks in
  one statement ordered by `COLLATE "C"`, one multi-row `INSERT ... SELECT FROM unnest(...)` each for items,
  derivations and job links). Item ids are assigned in Python (UUIDv7) so children in the same batch can
  reference their parents. Single-pipeline throughput (`scripts/bench_pipeline.py`, 5k messages, page 200,
  M3 laptop): 182 -> 220 msg/s. The rest is evidence I/O (pages and files) and dataset generation; the
  acceptance run scales out over worker processes instead.
- Every activity except `collect_pages` heartbeats in the background (`_ticking`), and every activity has a
  heartbeat timeout. Found by the acceptance run: a worker SIGKILLed inside `finalize_unit` (which had no
  heartbeat timeout) left its unit waiting for the 30-minute start-to-close. Timeout changes are
  replay-compatible (the golden histories still replay).
- Acceptance: `tests/integration/acceptance/test_resume_50k.py` drives `scripts/resume_soak.py`: 50,000
  messages (10 x 10 x 500, page 200), 3 `python -m edisc_worker` processes, 4 random SIGKILLs plus one
  kill-all/restart, oracle-exact. The 1M manual run is recorded in `docs/runs/`.
