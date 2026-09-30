# ADR 0010: Distributed rate limiting (Redis token buckets)

Status: Accepted (2026-09-30), implemented in M8 (`edisc_connectors_base.ratelimit`)

## Context
Many workers and processes call the same source APIs concurrently for the same tenant. Exceeding a
source's limits gets us throttled or, worse, gets the customer's app flagged. Worker clocks drift.
Redis can restart. None of this may cause unthrottled traffic or lost work.

## Decision
- **Bucket key** = tenant + source + workspace/org + API method, in Redis as
  `rl:{tenant:source:workspace:method}:bucket` / `:pause`. The shared `{…}` hash tag keeps both keys
  in one Redis Cluster slot.
- **Limits** come from configuration (`EDISC_RATE_LIMITS`, JSON: `{"source.method": {rate_per_second,
  burst}}`). There are no hardcoded limits. An unconfigured method raises
  `RateLimitNotConfiguredError` and is never unlimited.
- **Atomic check-and-take** is a single Lua script (token bucket). **Time comes from Redis `TIME`
  only.** Worker clocks are never used; the only local clock use is a *duration* when retrying a
  pause publish. Tested: 10 processes with clocks skewed by −1 day … +1 day produce the same rate.
- **New or lost buckets start empty.** After a Redis restart there is no burst: tokens refill at the
  configured rate.
- **Source back-pressure is shared.** A 429 / Retry-After seen by any worker calls `pause`, which sets
  a server-time pause (extended, never shortened) and drains the bucket. **Every** worker waits,
  whatever our bucket believed, and the throttled request is retried after the pause, not skipped
  (`call_with_limits`, the connector hook).
- **Fail closed.** If Redis is unreachable, `acquire` backs off and retries indefinitely. It never
  returns without a grant. `pause` keeps trying to publish until the pause would have ended; during an
  outage no worker can acquire anyway. Long waits call `on_wait(reason, seconds)` so Temporal
  activities keep heartbeating.

## Verification (`tests/integration/ratelimit`, 10 separate OS processes, grant times in server time)
- For **every** window between two grants: `count ≤ burst + rate × width`. Throughput stays ≥ 85% of
  the rate, and every worker gets work.
- The invariant holds with skewed worker clocks.
- One worker's injected 429 (Retry-After 1.5 s) produces zero grants from any worker during the
  pause. All 10 resume within 0.5 s afterwards.
- With Redis stopped for 3 s mid-run, every worker reports the outage and waits. There is a ≥ 2.5 s
  grant gap, no burst afterwards, and every work unit is processed exactly once.
- Mutation checks, each caught by these tests:
  - using worker clocks instead of Redis `TIME`;
  - a pause that only affects the worker that saw the 429;
  - failing open when Redis is down.

## Consequences
- + One place enforces source limits for all workers, jobs and processes of a tenant.
- − Redis is on the critical path of every fetch. An outage stops collection (by design) until it
  recovers.
- − The pause key survives restarts only as far as Redis AOF (`everysec`) does. A pause published in
  the last second before a crash can be lost, and the next request would then meet the source's own
  429 and pause again.
- − No fairness between concurrent jobs in one tenant yet: a large job can take most of the bucket
  (backlog).
