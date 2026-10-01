# ADR 0011: Thread replies in range whose parent is out of range

Status: **Proposed** (2026-09-30). Default pending product-owner confirmation; implemented as a
configurable policy in M9.

## Context
A collection has a date range. A thread reply can fall inside the range while its parent (and other
replies) fall outside it. Collecting only the in-range reply loses the context a reviewer needs to
understand it. Collecting the whole thread brings in material outside the requested range, which may
be outside the agreed scope of the matter.

## Options (`ThreadParentPolicy`, set per `CollectionScope`)
| Policy | Collected in addition to the in-range replies | Trade-off |
|---|---|---|
| `include_parent_and_thread` (**proposed default**) | The parent and the full thread, everything outside the range **marked out-of-range** | Full context. Out-of-range items are context only: excluded from reconciliation counts and flagged so review and production can filter them |
| `include_parent_only` | The parent only, marked out-of-range | Minimal context; sibling replies outside the range are missing |
| `replies_only` | Nothing | Strictly the requested range; replies appear without what they answer |

## Decision (proposed)
- Default `include_parent_and_thread`.
- Out-of-range context is fetched as separate `thread_context` batches (Slack: `conversations.replies`).
  The in-range unit's content and its expected count are identical under every policy.
- Context messages that are also in range repeat the unit's own messages and are deduplicated by
  idempotency key.
- Out-of-range items are marked (the `in_scope` flag lands with the pipeline in M11). They are never
  counted toward `expected` / `collected`, and they are listed separately in the collection report.
- The policy used is recorded on the scope and in custody events.

## Recommended answers (engineering, 2026-09-30), pending product-owner confirmation
1. **Default stays `include_parent_and_thread`.** Collect wide, produce narrow: context can be
   excluded later, but material not collected cannot be recovered after the source changes.
2. **Out-of-range context is collected and reviewable.** Whether it is producible is decided at
   production time, not at collection. Collection only marks it out-of-range.
3. **The mirror case is covered.** Under `include_parent_and_thread`, an in-range parent whose replies
   fall after the range end gets its full thread collected, with those replies marked out-of-range.
   Implemented in the dummy source (`Dataset.after_range_threads`) and tested.
   `include_parent_only` and `replies_only` add nothing in the mirror case.

Status remains **Proposed** until the product owner confirms these three answers.

- Several scopes per job (ADR 0005 amendment, M13.5): the policy is set per scope. A unit covered by
  several scopes uses the most inclusive of their policies, over the merged range of the conversation's
  scopes.
