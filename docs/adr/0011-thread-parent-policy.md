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

## To confirm with the product owner
1. Is `include_parent_and_thread` the right default, or should the default be the strictest
   (`replies_only`) with context as an explicit opt-in per matter?
2. Should out-of-range context be producible, or review-only?
3. The mirror case (parent in range, replies after the range end) is not covered by this policy today:
   replies outside the range are not collected. Should it be?
