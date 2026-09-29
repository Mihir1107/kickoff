# ADR 0004: Idempotency key and version fingerprint

Status: Accepted (2026-09-30)

## Context
Retries, restarts and overlapping scopes re-fetch the same data. Raw API bytes are not stable across
fetches (reply counters, presigned URLs, profile embeds), so hashing raw bytes would mint phantom
"versions". Edits must become new versions, never overwrites.

## Decision
- `idempotency_key = sha256(tenant_id || 0x1F || source || 0x1F || source_item_id || 0x1F || content_hash)`
  stored hex, `UNIQUE`. `source_item_id` is namespaced by the connector (e.g. `workspace/channel/ts`).
- Two hashes per item:
  - `raw_hash`: SHA-256 of the canonical JSON of the raw sub-document as stored (integrity).
  - `content_hash`: SHA-256 of the canonical JSON of the **version fingerprint** (identity of a version).
- The fingerprint is computed by the **normalizer** (connectors stay thin), per item type:

| item_type | Version-defining fields (in fingerprint) | Explicitly excluded |
|---|---|---|
| message | author external id; body text; rich body (blocks/HTML) as delivered; message subtype/type; thread root id; parent id; source edit marker (edited ts/etag-independent `lastModified` of content); deleted state + deletion ts; sorted list of attached file ids with their file content_hash | reactions; reply counts/reply users/latest reply; read receipts; pins/stars/bookmarks; presigned/expiring URLs; embedded user profiles; unfurl previews fetched by the platform |
| file | SHA-256 of file bytes (content_hash = byte hash); file name; mime type | download URLs, thumbnails, preview renditions |
| event (reaction snapshot) | parent message source id; sorted list of `(reaction name, sorted user ids)` | counts (derived) |

- **Reactions never create message versions**, but are never dropped: each observed reaction set is an
  `item_type=event` item, `source_item_id = <message id>#reactions`, `parent_item_id` = message item.
  A changed reaction set is a new event version (new content_hash). Other volatile-but-evidentiary
  signals (pins, read receipts) will follow the same pattern when a source provides them.
- Edits and deletions change the fingerprint → new `items` row with `version = previous + 1`,
  previous rows untouched. Version numbers are assigned in collection order per `source_item_id`.
- Each source's fingerprint mapping is documented here as it lands (dummy in Phase 1; Slack/Teams later).
  Changing a fingerprint definition is a normalizer version bump.

## Consequences
- + Re-fetches are free no-ops; edits/deletes produce versions; nothing volatile is lost.
- − Fingerprint definitions are consequential and must be reviewed per source.
