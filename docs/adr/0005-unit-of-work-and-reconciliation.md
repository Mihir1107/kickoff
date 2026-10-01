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

## Consequences
- + Gaps are localized to a conversation-day; retries are cheap.
- − Many tiny units for sparse sources; enumeration writes units to the DB in pages to keep history small.
