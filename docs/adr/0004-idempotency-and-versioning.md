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
| message | author external user id (not name); body text; rich body (blocks/HTML) as delivered; message type/subtype; thread root id; parent id; **deleted state**; sorted list of attached files as `[id, name, mimetype]` shown in the message (**not** their bytes; see below) | reactions; reply counts/reply users/latest reply; read receipts; pins/stars/bookmarks; presigned/expiring URLs; **author display name, avatar, profile embeds**; **link unfurls/previews**; **source change markers (etag, `edited.ts`, `lastModifiedDateTime`, deletion ts)** |
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

## Implementation (M10: `packages/normalizer`, migration 0009)

**Pure core.** `edisc_normalizer.slack` maps raw page bytes plus a prior-state snapshot plus
collected file evidence to derived records. It does no I/O, uses no clock, and is deterministic.
`edisc_normalizer.store` loads prior state and persists, inside the caller's batch transaction.

**Identity:**
- messages: `{workspace}/{channel}/{ts}` (channel + ts, never ts alone);
- files: `{workspace}/file/{id}`;
- directory profiles: `{workspace}/user/{id}#profile`;
- profile embeds: `…#profile-embed`;
- derived streams: `{subject}#reactions`, `#change`, `#observation`.

Fingerprints carry an `fp` tag (e.g. `slack.message/1`); changing a definition changes the tag.

**Files (decided 2026-10-01: option B; message fingerprint `slack.message/2`).**
- The message fingerprint carries attached files as `[id, name, mimetype]` as shown in the message,
  never their bytes.
- File bytes are versioned on the file item (`slack.file/1`: bytes SHA-256, name, mime type): new bytes
  under the same id make a new FILE version, not a message version.
- The pipeline attempts every referenced file before normalizing a page:
  - a file it has not attempted raises (a pipeline bug);
  - a file the source refuses (deleted, external/hidden, expired URL, permission) is a
    `file_unavailable` event `{file_id, status, reason, occurrence}` on `{workspace}/file/{id}#availability`.
    It is recorded, never raised, and the unit is marked as having a gap;
  - if the file is collected later, a `file_became_available` event follows. Availability **never**
    creates a message version.
- **Trade-offs considered:**
  - (A) a placeholder in the fingerprint replaced on availability would create versions that are not
    authored changes;
  - (A, frozen) would never reference the bytes from the message version;
  - with (B), a byte change under the same file id versions the file item instead of the message. The
    link is the message's file ids.

**Conversation access.**
- When a whole conversation becomes inaccessible (not_in_channel, channel_not_found, archived, access
  revoked), one `access_lost` event is recorded on `{workspace}/{conversation}#access`, with the
  source's error response as evidence. Per-message absence detection is suppressed for it.
- `access_restored` follows when the conversation answers again.

**Profile embeds** (`user_profile` in messages) show what the message displayed about its author.
They become `#profile-embed` identity snapshots, a set of observed states with no latest/revert
semantics because they are historical. Directory snapshots (`#profile`) are versioned with revert
detection. Messages reference user ids only.

**Reverts are observed.** The idempotency key would make a return to an earlier state (A→B→A) a
silent no-op. The normalizer emits a `#change` observation `{kind: reverted, from, to, occurrence}` for
messages, reaction snapshots and directory profiles.

**Absence is never deletion:**
- Only an explicit tombstone creates a deleted version.
- A message recorded before for a conversation-day and missing from a complete re-collection of that
  unit gets a `no_longer_observed` observation (event kind of the same name). It points at the unit's
  last page, where it was absent.
- If it reappears, it gets `observed_again`.
- Occurrence counters keep repeated disappearances distinct.

**Reactions:** a snapshot whenever present, and an empty snapshot when a live message that had
reactions no longer has them (Slack omits empty reaction lists). Tombstones say nothing about
reactions.

**In/out of range lives on the job link** (`job_items.in_scope`), never on the item. One item can be in
range for one job (e.g. a full-range collection) and out of range for another (e.g. a later-day-only
collection that pulled it in as thread context). Tested with exactly that pair of jobs.

**Derived records** live in `item_derivations`: one row per (item, normalizer version), append-only.
- Reprocessing stored raw pages with a newer normalizer adds derivation rows only.
- Items (keyed by fingerprint) and evidence objects, including their retention, are untouched.
- The normalizer version is also recorded on each item row that it created.

**Verified by** `tests/integration/normalizer`:
- After each of epochs 0, 1 and 2 (both dialects: tombstones, and omission of deleted messages), the
  database equals an oracle computed independently from the dummy model. That covers versions, change
  observations, reverts, reaction snapshots, directory and embed identity snapshots, files, and
  no-longer-observed / observed-again items.
- Idempotency (same page twice gives zero rows; overlapping pages give no duplicates) and reprocessing
  are also tested.
- Mutation-checked: a volatile field leaking into the fingerprint, missing hint observations, and
  missing absence detection each fail.

## Amendment (2026-10-02, M14.5): the "source" in the key is the platform, not the connector
`items.source` and the `source` in `tenant + source + source_item_id + content_hash` are the identity
namespace of what was collected (`Connector.item_source`), not the connector that collected it.
- Every Slack connector uses `slack`: the live Web API connector, the export connector (ADR 0014) and the
  dummy source, whose two dialects simulate the Slack Web API. The same message collected via the API
  and via an export is ONE item with one version when its fingerprint matches (tested: one dummy dataset
  collected both ways gives zero new items on the second job).
- Message ids stay `{workspace}/{channel}/{ts}`; for an export the workspace is the team id found in
  its `users.json`, so it matches the live connection's workspace.
- `collection_jobs.connector_version` and the custody `job_started` payload still name the connector.

## Consequences
- + Re-fetches are free no-ops; edits/deletes produce versions; nothing volatile is lost.
- − Fingerprint definitions are consequential and must be reviewed per source.
