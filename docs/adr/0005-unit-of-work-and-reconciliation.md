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
- Job final status:
  - `failed`: any unit `failed`, or the job could not finish.
  - `completed_with_gaps`: any unit `gap` or `surplus`.
  - `completed_unverified`: otherwise, if any unit is `unverifiable`.
  - `completed`: every unit `matched`, zero orphan evidence left `pending`.
- `completed_unverified` is shown **prominently** in the report and API (banner + per-unit list) and is
  never rendered or counted as a clean completion.

## Consequences
- + Gaps are localized to a conversation-day; retries are cheap.
- − Many tiny units for sparse sources; enumeration writes units to the DB in pages to keep history small.
