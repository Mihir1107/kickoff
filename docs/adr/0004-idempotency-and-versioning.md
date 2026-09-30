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
| message | author external user id (not name); body text; rich body (blocks/HTML) as delivered; message type/subtype; thread root id; parent id; **deleted state**; sorted list of attached file source ids with each file's content_hash | reactions; reply counts/reply users/latest reply; read receipts; pins/stars/bookmarks; presigned/expiring URLs; **author display name, avatar, profile embeds**; **link unfurls/previews**; **source change markers (etag, `edited.ts`, `lastModifiedDateTime`, deletion ts)** |
| file | SHA-256 of file bytes; file name; mime type | download URLs, thumbnails, preview renditions, etag/lastModified |
| event: reaction snapshot | parent message source id; sorted list of `(reaction name, sorted user ids)` | counts (derived) |
| event: change observation | parent message source id; hint name; old hint value; new hint value | observation time (metadata) |
| event: identity snapshot | external user id; display name; real name; email; avatar image hash; title; deactivated flag | presence, status emoji/text, timezone |

### Rules that apply to every fingerprint
- **Source change markers are hints, never identity.** etag, `lastModifiedDateTime`, Slack `edited.ts`
  and similar are used only to decide *whether to look closer* (e.g. delta queries, skip-unchanged
  optimizations). They are never part of `content_hash` or the idempotency key. They are still recorded
  on every item as metadata (`change_hints` JSONB, `edited_at_utc`, raw page).
- **A hint change without a content change is still visible at item level.** When any hint (Slack
  `edited.ts`, Teams `lastEditedDateTime`/`lastModifiedDateTime`, etag) differs from the latest recorded
  value for that message but the `content_hash` is unchanged, the normalizer emits a **change
  observation** event item: `source_item_id = <message id>#change`, `parent_item_id` = the current
  message version, fingerprint = `{parent, hint, old, new}`. Each distinct transition is a new event
  version; re-observing the same transition dedups. The first-ever sighting of a message has no "old"
  value and emits no observation. If the content also changed, the new message version carries the
  new hints and no separate observation is needed.
- **Deletion is version-defining; its timestamp is a hint.** A message going from not-deleted to
  deleted (or tombstoned) always creates a new message version. The deletion timestamp is recorded as a
  hint/metadata (`deleted_at_utc`) and never enters the fingerprint.
- **Text is hashed exactly as delivered.** No Unicode normalization (NFC/NFKC), no whitespace
  trimming/collapsing, no case folding, no entity decoding. RFC 8785 canonical JSON does not normalize
  strings either, so the bytes hashed are the source's code points, re-encoded as UTF-8.
- **Author presentation is not message content.** Display names, avatars and profile embeds are
  excluded from message versions and captured as **identity snapshot** event items
  (`source_item_id = user/<external id>#profile`), versioned when they change and linked to the
  custodian identity. What the reviewer saw at the time is therefore reconstructable without making
  every profile change look like a message edit.
- **Link unfurls/previews are excluded** from the message version: they are fetched by the platform,
  change over time and are not authored content. They remain preserved verbatim in the raw page object.

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
