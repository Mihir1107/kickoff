# ADR 0005: Unit of work = conversation × UTC day; reconciliation semantics

Status: Accepted (2026-09-30)

## Context
We need small, independently resumable, independently reconcilable pieces of work that map to how
sources paginate and how output is sliced (RSMF per conversation per 24h).

## Decision
- `unit_key = <conversation_id>/<YYYY-MM-DD>` with the day in **UTC**. Units are execution units, not
  output slices: renderers re-slice from items if a matter needs custodian-local days.
- A message belongs to the unit of its own `sent_at` (thread replies included, by their own timestamp).
- `expected_count` = number of distinct message `source_item_id`s the source reports for the unit
  (includes system/bot messages and replies; excludes files and events). `None` when the source cannot
  report it.
- `collected_count` = distinct message `source_item_id`s linked to the unit in `job_items`.
- Unit `recon_status`: `matched`, `gap` (collected < expected), `surplus` (collected > expected; reported,
  investigated, still not clean), `unverifiable` (expected is None), `failed` (unit could not complete).
- Additional unit outcomes (M11): `access_lost` (the conversation became inaccessible: a gap) and
  `not_applicable` (the directory unit). A unit with any **unavailable file** (`file_gaps > 0`) is a
  `gap` even when message counts match.
- Collected is counted from the job's links: distinct message items linked to the unit with `sent_at`
  inside the unit's day. Thread context from other days never counts.
- **Absence detection** (`no_longer_observed`) runs only:
  - for a unit that reconciled **clean** (`matched`, no file gaps); never for `gap`, `surplus`,
    `unverifiable`, `access_lost` or `failed` units;
  - against **earlier clean collections of the same conversation-day unit**. Messages seen only as
    thread context, or by jobs whose unit was not clean, are never the baseline. Scope differences
    therefore cannot produce spurious absence.

  A conversation that became inaccessible produces one `access_lost` observation and no per-message
  absence.
- Job final status:
  - `failed`: any unit `failed`, or the job could not finish.
  - `completed_with_gaps`: any unit `gap`, `surplus` or `access_lost`.
  - `completed_unverified`: otherwise, if any unit is `unverifiable`.
  - `completed`: every unit `matched`, zero orphan evidence left `pending`.
- `completed_unverified` is shown **prominently** in the report and API (banner + per-unit list) and is
  never rendered or counted as a clean completion.

## Amendment (2026-10-01, M13.5): jobs with several scopes
A job may have N >= 1 scopes. Each scope has a selector (channel / custodian / all), a date range and its
own thread-parent policy (ADR 0011). This replaces the single-scope guard of M11.1.

- **Units are the union over scopes.** A conversation-day covered by several scopes is ONE work unit,
  enumerated, fetched and reconciled once. `work_unit_scopes` records which scopes cover it.
- **In scope = in any range that applies to the conversation.** A message's link is `in_scope` when its
  timestamp lies in the range of any job scope that covers its conversation (taken from the scopes
  covering any unit of that conversation), whichever unit links it first. Context items outside every
  such range stay `in_scope = false`.
- **Thread context per unit:**
  - the date range is the union of the conversation's scope ranges, merged where they overlap or touch,
    and the context uses the merged interval that contains the unit's day;
  - the policy is the most inclusive policy among the scopes that cover the unit
    (`include_parent_and_thread` > `include_parent_only` > `replies_only`);
  - a unit covered by one scope therefore gets exactly that scope's policy, and a unit covered by
    several gets a superset of what each would fetch alone;
  - a parent outside every applicable range is fetched as context for each unit whose replies need it,
    but stored once (idempotency key) and linked once per job (`job_items` primary key).
- **Collected counts are per conversation-day across the whole job.** With several ranges, a message of
  day D can be linked first as thread context by a neighbouring day's unit (one link per item and job).
  It still counts for D. Found by a test with two partial-day ranges on one day, which reported a false
  gap before this rule.
- **Reconciliation and absence detection are per unit, unchanged.** Expected counts are per
  conversation-day, and absence detection runs only for clean units (this ADR).
- The custody `job_started` event lists every scope with its range and policy. A rerun job copies all
  scopes of the original, and its units keep the coverage they had. Approximation: for a rerun, the
  in-scope ranges of a conversation come from the scopes covering the re-run units only.

## Amendment (2026-10-02, ADR 0014): reconciliation against an uploaded export
- Units of an export job are its day files, keyed `{conversation}/{file date}` like every unit; the
  file date is a hint and items are placed and scoped by their own `ts`.
- A unit is `matched_against_archive` when every element of its day file became a message item and no
  file is missing; otherwise `gap`. The expectation is the element count taken at validation.
- A job whose units are all `matched_against_archive` ends `completed_against_archive`: never
  `completed`, `clean = false`, `clean_basis = "archive"`, and the ADR 0014 caveat is returned verbatim
  with the job, its reconciliation and every such unit.
- Absence detection ("no longer observed") never runs for export units: an export is a snapshot of
  unknown completeness, so a message missing from it says nothing.

## Consequences
- + Gaps are localized to a conversation-day; retries are cheap.
- − Many tiny units for sparse sources; enumeration writes units to the DB in pages to keep history small.
