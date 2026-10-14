# Phase 3 plan: live source connectors (Slack API, Teams)

Status: **not written yet.** This file collects the decisions already taken that the Phase 3 plan
must honour; the milestones themselves are proposed later, after M17. Related: ADR 0010 (rate
limits), ADR 0012 (auth pauses), ADR 0016 (install flows, the internal-app token exception and
Amendments 1 and 2), BACKLOG (the conversation metadata hard requirement).

## Decided so far

### Slack's rate limit on conversation history for non-Marketplace apps (2026-10-14, ADR 0016 A2.9)
- Since May 2025 Slack limits `conversations.history` for apps distributed outside the Slack
  Marketplace to **1 request per minute**; internal (customer-built) apps and Marketplace apps keep
  **Tier 3**. *(To confirm against Slack's current documentation at build: whether
  `conversations.replies` has the same limit, the per-request object cap that comes with it, and
  the date from which existing installations are affected.)*
- **The live connector detects the tier it is on and reports it as a job condition; it never crawls
  silently.** At 1 request per minute a channel with a long history takes days; a job that will take
  that long must say so (in the job's API answer, its custody chain and the report), not just run
  slowly. Detection uses what Slack tells us (the app's distribution status and the limits observed
  on the method, `Retry-After` included), never an assumption; the limits themselves stay in
  `EDISC_RATE_LIMITS` (ADR 0010) per method and tier.
- The report states the tier the job ran under (a new recorded fact in `job_started`, ADR 0018 §7.2
  pattern: new jobs only, UNKNOWN for older ones).

### The internal-app token exception is pending legal review (2026-10-14, ADR 0016 A2.10)
- Slack's terms treat templated custom apps (one app definition handed to many customers to create
  in their own workspaces) as commercial distribution. Whether ADR 0016 §6 (a customer-built
  internal app whose token is submitted once) is allowed for us is **pending legal review**.
- Until it is cleared: M17 step 7 (token submission) is not built, and **nothing is built that ships
  customers a ready-made Slack app manifest** (no manifest file, template, generator, or
  documentation of one).

### Carried from earlier decisions
- Conversation metadata as versioned `conversation_snapshot` items is a hard requirement of the live
  Slack connector (BACKLOG; CLAUDE.md M15).
- One external Slack workspace / Enterprise Grid org / Entra tenant per tenant of ours; moves are an
  operator action (ADR 0016 A4 and A2.3).
