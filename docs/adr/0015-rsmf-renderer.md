# ADR 0015: RSMF renderer (`edisc_renderers.rsmf`)

Status: **Accepted** (2026-10-03) with the review decisions below (§9). Steps 1–3 implemented and
approved (§10, §12 with the 2026-10-04 decisions in §13); step 4 implemented (§14, for review); step 5
not yet (M15). Implements M15 of
`docs/plans/phase-2.md` and decisions 5 and 4 there: renders by `matter_manager` and `tenant_admin` only
(`export.create`), audited; Relativity's validator stays out until the licence question is answered.

## Context
- RSMF 2.0 (Relativity Short Message Format) is an RFC 5322 message (`.rsmf`, an EML) with `X-RSMF-*`
  headers and exactly one attachment, `rsmf.zip`: base64, `Content-Disposition: attachment;
  filename="rsmf.zip"`.
  - The zip holds `rsmf_manifest.json` at its root, plus the attachment and avatar files the manifest
    names. An attachment's `id` is the name of a file in the zip.
  - The only required header is `X-RSMF-Version`.
  - Optional headers: `X-RSMF-Generator`, `X-RSMF-BeginDate`, `X-RSMF-EndDate`, `X-RSMF-EventCount`,
    and, from 2.0, `X-RSMF-Application`, `X-RSMF-Custodian`, `X-RSMF-Participants`,
    `X-RSMF-AttachmentCount`, `X-RSMF-EventCollectionID` (Relativity's RSMF documentation).
- The manifest schema is Relativity's `rsmf_schema_2_0_0.json` (JSON Schema draft-07), in the BSD-3 repo
  `relativitydev/rsmf-validator-samples` (Copyright (c) 2016, kCura LLC).
  - Required at the top level: `version`, `participants` (`id`), `conversations` (`id`, `platform`,
    `participants`; `type` is `direct` or `channel`) and `events` (`type` is one of `message`,
    `disclaimer`, `join`, `leave`, `history`, `unknown`).
  - Events may carry `id`, `parent`, `body`, `participant`, `conversation`, `timestamp` (date-time),
    `deleted`, `importance`, `reactions` (`value`, `count`, `participants`), `attachments` (`id`,
    `display`, `size`), `edits` (`participant`, `timestamp`, `previous`, `new`), `custom` (name/value
    pairs), `direction` and `read_receipts`.
- Relativity's validator SDK is proprietary. It is not used until the organisation confirms a licence.

## Decision

### 1. Input: normalized items, never raw pages
- **What a render reads:** a job's linked items, through the latest normalizer derivation, plus the
  file evidence those items reference.
  - Messages, with their earlier versions as edits.
  - Events: joins and leaves; reaction snapshots (the latest for each message); identity snapshots
    (for participant names); file availability.
  - Raw evidence is read only to embed file bytes, always by pinned version (or from the locked export
    archive for exports).
- **Scope:** every in-scope message of the job. Out-of-scope items appear ONLY as marked thread
  context (§3), and only when the render option `include_context` is true (the default). The option is
  recorded in the render's custody stream (§7), because producibility is decided at production time.
- The renderer is a library: `render(slice_inputs) -> bytes stream`. It imports no database or cloud
  code. A thin loader in the worker builds its inputs, so golden tests run on plain data.

### 2. Slicing and the cap
- **Slices:** one conversation per 24 h. UTC by default; with a matter time zone, `local_day_bounds`
  (DST days are 23 or 25 h long). An event belongs to the slice containing its own timestamp.
- **Cap:** at most **10,000 events** per file, counting context events. A larger slice is split in
  timestamp order into parts. The `part N of M` labels are fixed by the data alone, so a re-render
  gives the same parts.
- An empty slice produces no file. The render report lists the slices it produced.

### 3. Mapping to the manifest
- **`version`:** `"2.0.0"`. `eventcollectionid` is a stable slice id:
  `{job}/{conversation}/{day}/{part}`, hashed to a UUID.
- **Participants:**
  - one per user referenced by an event, a reaction or an edit;
  - `id` and `account_id` are the Slack user id;
  - `display` comes from the identity snapshot in force at the slice (falling back to the id);
  - `email` only if it validates as an email;
  - `custom`: team id, bot/app flags, deactivated;
  - avatars are not embedded in M15 (avatar files are not collected yet).
- **Conversation:** one per file.
  - `id` is the Slack conversation id; `platform` is `"slack"`; `display` is the channel name.
  - `type`: `direct` for DMs and group DMs, `channel` for public and private channels. The original
    Slack type is always in `custom` as `slack.conversation_type` (`im`, `mpim`, `public_channel`,
    `private_channel`), so a group DM rendered as `direct` stays identifiable.
  - `participants`: the members when known (export metadata, conversation info), else those observed.
  - `custodian`: the matter custodian mapped to the collected identity, when one is mapped.
  - `custom`: workspace id, kind (`public`, `private`, `dm`, `mpim`), shared/external.
- **Events:**
  - **Messages:**
    - `type: "message"`; `id` is the Slack `ts` (unique within the conversation).
    - `parent`: the thread root `ts` for replies. `body` is the text exactly as collected, with Slack
      mentions kept as written.
    - `participant`, `conversation`, and `timestamp` (UTC with microseconds,
      `2026-01-05T09:00:00.000100Z`).
    - `deleted: true` for tombstones, with empty body.
    - `reactions` from the latest snapshot.
    - `edits`: the earlier versions of the same message in order, as
      `{participant, timestamp: edit ts, previous, new}`.
    - `attachments`: one per file (§4).
  - **Joins and leaves:** `channel_join` / `channel_leave` subtypes become `type: "join"` / `"leave"`.
  - **Bot and app messages:** `message`, with the bot or app as the participant.
  - **Anything uninterpretable:** `type: "unknown"` with the raw subtype in `custom`. Never dropped.
  - **Provenance in `custom`** (every event): the item's idempotency key, content hash, source item id
    and version, and `edisc.in_scope`.
  - **Thread context** (ADR 0011), with `include_context` (the default): a reply in this file whose
    root sits in an earlier slice or part, or outside the job's scope, is preceded by its root. The root
    carries `custom` `edisc.context = thread_root_outside_file` (or `thread_root_out_of_scope`), so every
    `parent` resolves inside the file.
  - With `include_context = false`, no out-of-scope item is rendered. A reply whose root is not in the
    file keeps its `parent` only if that root is in the file; otherwise `parent` is omitted and `custom`
    `edisc.parent_not_rendered = <root ts>` records it, so the thread is never silently cut.
- **Determinism:**
  - events are sorted by (timestamp, id); participants by id;
  - the manifest is RFC 8785 canonical JSON, UTF-8;
  - empty optional fields are omitted, never null.

### 4. Attachments
- **Collected files:** each is put in the zip under a flat, safe name:
  `{file_id}_{sanitized original name}`. The name is NFC-normalized, path separators and control
  characters are replaced, it is capped at 200 bytes, and the extension is kept. That name is the event
  attachment's `id`; `display` is the original name and `size` the byte size. Bytes are streamed from
  the pinned evidence, never held whole.
- **Unavailable files:** a placeholder text file `{file_id}_UNAVAILABLE.txt` states the reason (deleted,
  expired link, permission, ...), with custom `edisc.file_unavailable` on the event. A gap is never
  silent.

### 5. The EML
- **Envelope:** RFC 5322 with CRLF line endings, MIME `multipart/mixed`.
  - A short `text/plain` part summarizes the slice; then `rsmf.zip` (`application/zip`, base64 in
    76-character lines, `Content-Disposition: attachment; filename="rsmf.zip"`).
  - `Date` is `X-RSMF-EndDate`. `Message-ID` is `<{source-hash}@rsmf.edisc>`, and the MIME boundary is
    also derived from the source hash. No wall clock is used anywhere.
- **Standard headers:**
  - `X-RSMF-Version: 2.0.0`; `X-RSMF-Generator: edisc-renderers/<semver>`;
  - `X-RSMF-BeginDate` / `X-RSMF-EndDate`: the first and last event timestamps;
  - `X-RSMF-EventCount`; `X-RSMF-AttachmentCount`; `X-RSMF-Application: Slack`;
  - `X-RSMF-Custodian` (when mapped); `X-RSMF-Participants` (display names, folded per RFC 5322);
  - `X-RSMF-EventCollectionID`.
- **Custom headers:**
  - `X-RSMF-CollectionId` (job id) and `X-RSMF-ConnectorVersion` / `X-RSMF-NormalizerVersion`.
  - `X-RSMF-RendererVersion`: the renderer's semver (`edisc_renderers.rsmf.RENDERER_VERSION`), the same
    as in `X-RSMF-Generator`, as its own machine-readable header. Byte-identical output is promised
    for the same inputs AND the same renderer version. Any change to the output bytes bumps it, and the
    golden tests carry it.
  - `X-RSMF-IncludeContext`: `true` or `false`, the render option in force.
  - `X-RSMF-SourceHash`: the RFC 6962 Merkle root over the sorted `(idempotency key, content hash)` of
    every item rendered in the file, context included. It is the same construction as the custody batch
    roots, so it can be recomputed from the custody package.
  - `X-RSMF-Slice`: conversation, day and time zone. `X-RSMF-Part: N/M`.
  - `X-RSMF-CompletenessBasis`: `source` or `archive`. For `archive`, the ADR 0014 caveat goes in the
    text part, verbatim.
- **Non-ASCII:** header values are encoded per RFC 2047 where needed, folded at 78 characters, and
  deterministic.

### 6. Determinism
The same inputs give byte-identical files:
- zip entries sorted by name, with timestamps fixed at 1980-01-01 00:00 and fixed permissions;
- entries STORED (no compression), no extra fields, no comments. *Decided 2026-10-04, replacing
  "deflate at a fixed level":* deflate output depends on the zlib build (zlib versions and zlib-ng
  produce different bytes for the same input and level), so it would tie byte identity to the machine.
  STORED makes the zip bytes independent of the zlib build, so the same inputs give the same file on
  any machine. The cost is size: manifests are text and compress about 5x, while most attachments are
  already compressed;
- canonical JSON and derived boundary and ids;
- **pinned runtime inputs** (decided 2026-10-04): time zones come only from the pinned `tzdata` Python
  package, never the system zoneinfo (`edisc_renderers.rsmf.runtime.load_zone`; a test poisons the
  system path to prove it). Python is pinned to the patch version in `.python-version`, which fixes
  `unicodedata` (NFC and the character categories used in zip names). `unicodedata.unidata_version`
  and the tzdata (IANA) version go in the render summary (`Reconciliation`) and the render's custody
  stream.

A golden test compares bytes (`tests/golden/rsmf/<key>/`). The key is the renderer version plus the
Unicode and tzdata versions (`golden_key()`, e.g. `1.0.0_unicode-15.0.0_tzdata-2026e`). A change to
the bytes without a version bump fails CI, and a new Python or tzdata pin needs a newly recorded
generation. The renderer version is recorded in every file's headers and in the render's custody
stream.

### 7. Storage, custody, API
- **Storage:** renders are derived products, never evidence. They go to
  `t/{tenant}/productions/{render}/...`, hashed while written and locked for the matter window, as
  registry rows of a new kind `production`.
- **Custody: one stream per render** (stream id = render id). A sealed job chain is NEVER reopened: seals
  stay final.
  - The render stream's FIRST event, `render_started`, references the sealed job it renders:
    - the job id;
    - the job chain's final head hash and seq;
    - the seal anchor (WORM key and pinned version);
    - the job's status and completeness basis;
    - the renderer version and the render options (`include_context`, time zone, cap).
  - The render refuses to start unless the job is sealed and its chain verifies up to that head.
  - Then the files in bounded `render_files_batch` events, each with a Merkle root over its files
    (*amended 2026-10-04, replacing one `rsmf_rendered` event per file; §14*).
  - Then `render_completed` (file count, reconciliation summary, root over the batch roots) and a seal,
    anchored to WORM like a job chain.
  - The tenant audit stream records who asked for it (`audit.render_requested`: actor, job, options),
    and when it ends, the render's final head (`audit.render_completed`).
- **API:**
  - `POST /v1/jobs/{id}/renders` (`export.create`: matter_manager, tenant_admin; recent sign-in per
    ADR 0016). The body has the time zone and `include_context` (default true). It runs as a Temporal `RenderWorkflow` (queue
    `renders`, idempotent per (job, options) with an `Idempotency-Key`).
  - `GET /v1/renders/{id}` returns status, files and the custody head.
  - Downloads go through the audited content endpoint with purpose `rsmf`.
  - Reviewers can only preview (M16).
- **Jobs that can be rendered:** finished, sealed jobs only. The render records the job's status and
  completeness basis.

### 8. Validation (CI)
- The schema is vendored at `packages/renderers/src/edisc_renderers/rsmf/schema/rsmf_schema_2_0_0.json`
  with the repo's BSD-3 `LICENSE` and a `SOURCE.md` (URL, commit
  `c717cd322264b46115d27d034a6107c8c91043d8`, file SHA-256).
- Every manifest written in tests is validated with `jsonschema` (draft-07, format checks on). Render
  time validates too: an invalid manifest fails the render loudly.
- **Structural EML checks** (Python's `email` parser):
  - exactly one attachment, named `rsmf.zip`, base64;
  - the required header, and the headers consistent with the manifest (counts, dates);
  - `rsmf_manifest.json` at the zip root;
  - every attachment id exists in the zip, and every zip file except the manifest is referenced;
  - every `parent` resolves inside the file, and every referenced participant exists.
- **Fixture corpus from the dummy oracle, both dialects:**
  - emoji, RTL, zero-width and combining text; a very long message; an attachment-only message;
  - tombstones; edits across epochs; cross-day and cross-slice threads; reactions; joins; bots;
  - unavailable files; a DST day in a matter time zone; the 10,000 split (one 10,001-event slice);
  - an export-dialect job with the archive caveat.
- Relativity's validator: not in CI until the licence is confirmed (`docs/plans/phase-2.md`,
  decision 4).

## Consequences
- Plus: every rendered event traces to a collected item (keys in `custom`, the Merkle root in a header
  checkable against custody), and renders are reproducible byte for byte.
- Plus: gaps (unavailable files, deleted messages, archive-relative completeness) stay visible in the
  render.
- Minus: a reply whose root is outside the file repeats the root as context: the file is slightly larger,
  but it stays readable.
- Minus: avatars are not rendered until avatar files are collected.

## 9. Review decisions (2026-10-03)
1. **Custody:** each render has its own custody stream plus tenant audit events. The stream's first
   event references the sealed job (job id, final chain head hash, seal anchor). Seals stay final; a
   sealed job chain is never reopened.
2. **Group DMs:** RSMF type `direct`; the original Slack conversation type (`mpim`) is recorded in
   `custom`.
3. **Out-of-scope messages:** they appear only as marked thread context, and only when the render
   option `include_context` is true (the default). The option is recorded in the render's custody
   stream: producibility is decided at production time.
4. **Renderer version:** `X-RSMF-Generator: edisc-renderers/<semver>` plus `X-RSMF-RendererVersion:
   <semver>`. Byte-identical output is tied to a specific renderer version.

## 10. Implementation notes, steps 1 and 2 (2026-10-04, for review)
Steps 1 (vendored schema) and 2 (pure renderer) are done. **Approved 2026-10-04**, including the
items below (3, 4 and 7 were the gaps filled in code).

1. **Zip entries are STORED** (approved 2026-10-04 and moved into §6). Evidence files are streamed
   once, so they use a data descriptor (flag bit 3). The manifest and placeholders carry their CRC in
   the local header.
2. **Render reconciliation (review requirement).**
   - Unit of reconciliation: the message subject (source item id). Earlier versions are its `edits`,
     and the latest reaction snapshot and the files fold into the same event.
   - `Reconciler` re-reads each manifest from its bytes and checks every slice:
     - the primary events, as a multiset of subjects, equal the slice's in-scope input (exactly once);
     - parts run 1..M and every file is at or under the cap;
     - every context event is marked, is a root that a primary event of the same file needs, and its
       marker agrees with `edisc.in_scope`.
   - At the job level, each (conversation, day) is accepted once. `finish(expected count, expected
     digest)` compares with an order-independent subject digest (sum of SHA-256 mod 2^256). The loader
     derives the expected values from the job's in-scope links, so a substitution (one item missing,
     another duplicated) fails even when the counts agree.
   - Summary (`Reconciliation.as_payload()`, goes into the render custody stream in step 4):
     `items_in`, `events_out`, `context_events`, `context_events_out_of_scope`, `edits`,
     `attachments`, `unavailable_attachments`, `parents_not_rendered`, `files`, `slices`,
     `subject_digest`.
3. **Thread context:**
   - A reply whose root is not a primary event of the same file gets the root as context, wherever the
     root is (an earlier slice, an earlier part, out of scope).
   - The loader must name each referenced root: give it in `roots`, or declare it in `missing_roots`
     (the job does not hold it). Anything else is a loader bug and raises.
   - A missing root is never invented. The reply carries `edisc.parent_not_rendered` plus
     `edisc.parent_not_rendered_reason`: `context_excluded` (include_context off) or `not_collected`
     (the job lacks the root).
   - Splitting counts the context a part needs, so no file exceeds the cap.
4. **Edits:** one entry per consecutive pair of versions, `previous`/`new` always present (an empty
   string is real content here). The timestamp is the new version's `deleted_ts` for a deletion, or
   its `edited_ts` hint when that hint changed; otherwise it is omitted. A tombstone therefore keeps
   the collected text in `edits`, and `deleted: true` comes with no body.
5. **Custom names:**
   - On events: `edisc.idempotency_key`, `edisc.content_hash`, `edisc.source_item_id`, `edisc.version`,
     `edisc.in_scope`, `edisc.prior_version_keys`, `edisc.reactions.idempotency_key` and
     `.content_hash`, `edisc.file_unavailable` (`<file id>: <reason>`), `edisc.context`,
     `slack.subtype`.
   - On participants: `slack.team_id`, `slack.is_bot`, `slack.is_app_user`, `slack.deactivated`.
   - On conversations: `slack.conversation_type`, `slack.kind`, `slack.workspace_id`,
     `slack.is_shared`, `slack.is_ext_shared`.
   - Name/value pairs are sorted, and empty values are omitted (the schema needs `minLength: 1`).
6. **`X-RSMF-SourceHash` leaves:** every version of each rendered message (current and earlier), its
   reaction snapshot, and the file item (or `file_unavailable` item) of each attachment, context
   included. Deduplicated by key; the same key with two content hashes raises.
7. **Envelope details the ADR left open:**
   - `From: rsmf@rsmf.edisc`, plus a `Subject` naming the conversation, day, zone and part;
   - `X-RSMF-Participants` lists display names in participant-id order, joined with `, `;
   - ASCII header values fold at spaces only, so a single long token (`Message-ID`, the source hash)
     may exceed 78 characters but never 998. Non-ASCII values use UTF-8 B encoded-words split on
     code points;
   - the text part is base64 UTF-8;
   - file name `{conversation}_{day}_part{NNN}of{MMM}.rsmf`.
8. **Zip names:** NFC, then `_` for path separators, Windows-reserved characters, control, format,
   private and unassigned code points, and Unicode spaces other than U+0020. Leading and trailing dots
   and spaces are trimmed. A name collision between two file ids raises.
9. **Limits:** no ZIP64. Until §11 is implemented, a file over 4 GiB or 65,535 entries raises
   `ZipLimitError` before any byte is written. Evidence size and SHA-256 are verified while streaming
   (`EvidenceMismatchError`).
10. **Runtime dependence:** pinned and recorded (§6).

## 11. Oversized attachments (decided 2026-10-04; implement after step 5, required before production)
An attachment must never fail a render because the zip would get too large. Oversized attachments
leave the zip and travel next to it. This comes ahead of ZIP64, which stays in the backlog.

- **Which attachments leave the zip:** each part's zip size and entry count are known before any byte
  is written (sizes come from the inputs). While the planned zip exceeds the limits (4 GiB minus the
  manifest and headroom, or 65,535 entries), attachments move out one at a time, largest first,
  ties broken by file id. The choice depends only on the data, so the bytes stay deterministic. A
  single attachment over the limit always leaves.
- **In the RSMF:** the event keeps an attachment whose id is a placeholder text file
  `{file_id}_EXTERNAL.txt`. The placeholder states the original name, size, SHA-256, the reason
  (`exceeds_rsmf_zip_limit`) and the native's path in the render package. The event's `custom` gains
  `edisc.file_external = <file id>: sha256:<hex>`. `display` is the original name.
- **The native:** written once per render at `t/{tenant}/productions/{render}/natives/sha256/<hex>`,
  content-addressed and streamed from the pinned evidence version, with the hash verified on read and
  again on write. It is a production output like the `.rsmf` files: locked under the matter and
  recorded with its VersionId and our SHA-256. A download or export of the render includes it, and
  the render's file list references it by hash.
- **Custody and reconciliation:** one `native_written` event per native (SHA-256, size, key, version,
  referencing files). `rsmf_rendered` lists the external references. The summary gains
  `external_attachments`. The reconciler fails if a referenced native was not written with the
  recorded hash.
- **Not a gap:** the bytes are delivered, so completeness is unchanged. `X-RSMF-SourceHash` still
  covers the file item. The collection report (M16) lists the external natives.

## 12. Implementation notes, step 3: loader and storage (2026-10-04, for review)
Code: `edisc_worker.render_loader` (loader), `edisc_worker.render_store` (orchestration),
`EvidenceWriter.write_production`, migration 0024. Tests: `tests/integration/renders`,
`tests/integration/api/test_export_render.py`.

1. **Only finished, sealed jobs** are loaded (`RenderRefusedError` otherwise). Verifying the job chain
   up to its head is step 4, with the render's custody stream.
2. **Query by id:**
   - per conversation, the job's links come from `job_items` by the conversation's unit keys, then
     items by id in chunks of 5,000; days and types are filtered in Python;
   - a message can be linked under another unit of its conversation (a thread batch; first link
     wins), which is why the index covers all of a conversation's units;
   - scope comes from the link, and a subject whose versions are linked with different scope raises.
3. **As of the job:**
   - message versions run up to the highest version linked to the job (earlier versions from earlier
     jobs become `edits`);
   - reaction snapshots, files, availability and identity snapshots are the versions collected up to
     the job's `finished_at`;
   - each item uses its derivation with the highest normalizer version; `derived_hash` is re-checked,
     so an altered derivation fails;
   - `X-RSMF-NormalizerVersion` lists every version the job's links have.
4. **Files:** collected bytes at any time up to the job win over a later refusal (we hold them). The
   registry SHA-256 must equal the file item's `raw_hash` and its derivation, and the evidence must be
   complete and pinned. Without bytes, the latest `file_unavailable` event gives a placeholder (named
   by file id: the message derivation does not carry file names). A file with neither raises.
5. **Identities:** directory snapshots (`#profile`) in collection order. The first is in force from the
   start, each later one from its `collected_at`, the time we observed the change (a lower bound
   for when it happened). Profile embeds are not used yet. Bot/app flags are not in the profile
   fingerprint, so they are omitted.
6. **Verified on read (review requirement):**
   - every page or archive entry behind an item of a slice is read by its pinned VersionId (archive
     entries are decompressed from the locked export's pinned version, with CRC and size checked) and
     must match the registry's SHA-256 and size;
   - every item's sub-document at `json_path` must re-hash to `items.raw_hash`;
   - all of this happens before the slice reaches the renderer, outside any DB transaction, and each
     object once per render;
   - file bytes stream by pinned version and are checked by the renderer as they pass; a mismatch
     aborts that upload, so no object exists and the row stays `pending`;
   - tested by tampering a page hash, an item's `raw_hash` and a derivation (all fail before anything
     is stored), and by flipping a byte on the file read path.
7. **Two passes:** pass 1 renders the manifests, with no bytes, and reconciles the whole job against
   `count(DISTINCT in-scope message subject)` and its digest, computed by the database. Only then does
   pass 2 re-render and store, and each file must equal pass 1's record (name, slice, part, source
   hash, counts). A reconciliation failure therefore leaves nothing stored. Pages verified in pass 1
   are not re-read.
8. **Storage:**
   - kind `production`, key `t/{tenant}/productions/{render}/{file name}`, tied to the rendered job,
     so its matter owns retention (`effective_retain_until`; the retention extension job covers it);
   - Object Lock COMPLIANCE at creation, If-None-Match;
   - our SHA-256 is persisted as `source_sha256` with origin `render` before the commit, and the row
     records the VersionId;
   - the writer takes a stream factory: a retry with the same render id re-renders and must hash to
     the stored row (an incident otherwise), or completes a pending row against a stored version.
9. **Memory:** one conversation's link index (subject, ts, version, scope) plus one slice of items and
   one page at a time. A 64 MiB attachment peaks under 8 MiB through zip and base64 (test); the upload
   buffers one part.
10. **Conversation metadata (open, for review):** only export jobs record a conversation's type and name
    (`export_conversations`). For live and dummy jobs the RSMF `type` (optional in the schema) and the
    name are omitted, and `edisc.conversation_metadata = not_collected` says so. Members fall back to
    the observed participants. Collecting conversation metadata (type, name, members) as normalized
    items is a connector + normalizer change for a decision (backlog).
11. **Custodian:** set when exactly one custodian-type scope of the job covers the conversation.

## 13. Review decisions on §12 (2026-10-04), implemented (renderer 1.1.0, normalizer 0.2.0)
1. **Conversation metadata:** the versioned conversation snapshots of ADR 0004's amendment are read
   as of the job: type, name, members, topic, purpose, archived, shared. Every name the conversation
   had goes into conversation `custom` as `edisc.known_name`, so a renamed channel stays findable
   under old names. Exports still use `export_conversations`. A job with neither keeps the type
   omitted and `edisc.conversation_metadata = not_collected`. Collecting this metadata is a hard
   requirement of the Phase 3 live Slack connector.
2. **Identities (§12.5) approved,** and every participant carries `slack.user_id` plus every name
   from any of its snapshots (`edisc.known_name`, display and real names), so old names stay
   searchable. `display` stays the name in force at the slice.
3. **File bytes held beat a later refusal (§12.4):** approved.
4. **Unavailable placeholders** are named from the message's own attachment reference (part of
   the version fingerprint; now in the derivation): `{file_id}_{sanitized name}.UNAVAILABLE.txt`,
   at most 200 bytes. The text gives the safe name, the file id, the name as shown in the message,
   the reason and the recording item. `display` is the original name.
5. **Custodians:** every custodian scope covering the conversation is listed in conversation
   `custom` as `edisc.custodian` (sorted). The RSMF `custodian` field is set only when there is
   exactly one; `X-RSMF-Custodian` lists every custodian's display name.

These change the output bytes: `RENDERER_VERSION` 1.1.0, goldens in
`tests/golden/rsmf/1.1.0_unicode-15.0.0_tzdata-2026e/`. The 1.0.0 generation stays as history.

## 14. Step 4: render workflow, render custody stream, API (2026-10-05, for review)
Implements the step 4 plan approved 2026-10-04 with its four decisions. Code: `edisc_worker.renders`
(the lifecycle and activities), `RenderWorkflow` (`edisc_worker.workflows`, queue `renders`, id
`render-{render_id}`), `edisc_api.routes.renders`, `edisc_custody.render_files` / `render_package` /
`render_export`, migration 0026. Tests: `tests/integration/renders/test_render_workflow.py`,
`tests/integration/api/test_renders.py`, `tests/integration/custody/test_render_package.py`,
`tests/unit/custody/test_render_files.py`; Temporal goldens `render-clean`, `render-failed`.

1. **Records:** `renders` (status `requested -> rendering -> rendered -> completed`, or `refused` /
   `failed`; final states never change except the seal, recorded once; a guard trigger enforces the
   transitions and the immutable identity and job reference) and `render_files` (insert-only, one row
   per output file, tied to its batch event by a deferred FK, like `job_items`).
2. **Identity and deduplication (decision 2):** (job, options hash, renderer, Unicode and tzdata
   versions). A partial unique index allows one live render per identity; failed and refused renders
   do not count. Concurrent identical requests: `INSERT ... ON CONFLICT DO NOTHING`, then read the
   winner. The API answers 201 when it created the render (or replays the key that did), 200 when it
   returned the live one. `audit.render_requested` records every request (with `created`); a key
   replay is the same request and is not recorded again.
3. **The render's custody stream (stream id = render id):** every event has `render_id` set and
   `job_id` NULL (a check constraint: `render_id = stream_id`, which the event hash covers; a sealed
   job's chain is never appended to). Events:
   - `render_started`: render id, versions, options and options hash, requester, and `job`: id,
     status, completeness basis, final head (seq, hash) and seal anchor (key, VersionId). Before it,
     the job must be sealed, its matter and client open, its chain verify up to that head with the
     seal required, and the seal key be exactly one object version (listed from S3, never the DB)
     whose body anchors that head. Otherwise `render_refused` (reason `job_not_sealed`,
     `matter_closed`, `client_closed`, `chain_verification_failed`, `seal_mismatch`) is the only event.
   - `render_files_batch` (decision 1): at most `EDISC_RENDER_FILES_BATCH_SIZE` (500) files: batch
     index, first ord, file count and the RFC 6962 root over the files' records in render order
     (leaf = canonical JSON of name, slice, part, VersionId, SHA-256, size, source hash, counts;
     `render_files.FILE_FIELDS`).
   - `render_completed`: file count, batch count, the root over the batch roots, the reconciliation
     summary and the number of verified objects.
   - `render_failed`: the error class and text, the status it failed from, progress so far; an alert.
   - Then the seal: a forced anchor of the final head, recorded on the render together with the tenant
     audit event (`audit.render_completed` / `.render_refused` / `.render_failed`) in one transaction.
   - Render lifecycle events trigger anchors like job lifecycle events.
4. **Resumability:** each activity acts only from the status it starts from and moves the status in
   the same transaction as its event (the status is the fence, not a value that can repeat; see the
   ABA fix in ADR 0006). A batch is committed only when `batches_done` equals its index; a batch an
   earlier attempt committed must re-render to exactly the recorded records, or the render fails as
   an integrity incident. The render id sets the storage keys, never the bytes, so a retry dedups
   against the stored objects. Crash tests at every boundary and a real SIGKILL of the worker process
   end with the same files as an independent in-memory rendering.
5. **Failures:** integrity errors (render inputs, evidence, reconciliation, renderer, file records)
   are class `RenderIntegrity` (non-retryable); anything that fails for good leads to `fail_render`,
   which is retried without limit: a render never ends without a sealed record. A worker whose
   renderer, Unicode or tzdata version differs from the render's identity refuses to render it (an
   integrity failure, so the render fails and a new request renders on the current versions): bytes
   are only promised for the versions the render records.
6. **Retention:** render anchors carry the render id (`evidence_objects.render_id`, job id NULL), as
   do productions (which also keep the job id). Anchor retention and the retention extension resolve
   render -> job -> matter.
7. **API (decisions 3, 4):** `POST /v1/jobs/{id}/renders` (`export.create`), `GET /v1/jobs/{id}/renders`,
   `GET /v1/renders/{id}`, `GET /v1/renders/{id}/files`, `GET /v1/renders/{id}/custody/verify`
   (`custody.read`: auditors see status and custody), `GET /v1/renders/{id}/files/{ord}/content`
   (`export.read`, completed renders only; audited and anchored before any byte; re-hashed while
   streaming, a mismatch aborts and raises an alert). `export.create` and `export.read` belong to
   matter managers (and tenant admins); reviewers, collectors, auditors and client admins have
   neither. The generic `/v1/evidence/{id}/content` never serves a `production`.
   Recent sign-in: `edisc_api.auth.require_recent_sign_in(caller)` is called in the create route
   before any change; it does nothing until the M17 sessions exist (ADR 0016 §4 now lists render
   creation).
8. **`edisc-verify` render packages** (`edisc-render-package/1`, exported by
   `export_render_package`): the render chain with every batch root and the completed totals, the
   start of the stream and its reference to the job seal (included as read from WORM, which must
   anchor exactly the referenced head), every output file's SHA-256 and size (embedded, or supplied
   with `--file`), nothing unlisted in `outputs/`; with `--job-package`, the job's custody package is
   verified too and must hold the referenced head and seal.
9. **Not in step 4:** the full render crash matrix and fixture corpus (step 5); oversized attachments
   as external natives (§11); a package download endpoint (the exporter is a library function).

## 15. Operations: stuck sealing and version routing (2026-10-05, for review)
1. **Stuck sealing (migration 0027).** Sealing stays retried without limit. Every failed seal attempt
   is counted on the render (`seal_failures`, `last_seal_error`). When the failures reach
   `EDISC_RENDER_SEAL_STUCK_ATTEMPTS` (5), or the render has been final for
   `EDISC_RENDER_SEAL_STUCK_SECONDS` (900) without a seal, `sealing_stuck_at` is set once and one
   `render_sealing_stuck` alert is raised. The elapsed check also runs at the start of every attempt,
   so attempts killed before they could record a failure still count. The API shows `sealing_stuck`
   (stuck and not yet sealed), `sealing_stuck_since`, `seal_failures` and `last_seal_error`; after a
   later seal, `sealing_stuck` is false and the timestamp stays as history.
2. **The anchor sweeper covers render streams.** `due_anchor_streams` selects any chain head with an
   overdue or idle unanchored tail, render streams included, and the anchor's retention and
   `render_id` resolve through the render (tested with a worker that died after unanchored batches).
3. **Version routing: a version-keyed task queue, not Temporal worker versioning.** A render runs on
   `renders.r<renderer>.u<unicode>.tz<tzdata>`, the queue of the versions it recorded at creation;
   each worker polls the queue of its own runtime versions (`RenderActivities.task_queue`).
   - Why not build-id versioning (Worker Deployments): it routes by code build, sending new workflows
     to the deployment marked current and keeping started ones on their build. A render must be
     routed by data (the triple fixed when it was created, possibly by an API on another build), and
     several triples may be served at once during a rollout. A queue per triple says exactly that,
     needs no deployment operations (registering builds, promoting a current version) and works on
     the pinned server unchanged.
   - The fail-on-mismatch check stays as the safety net: a misrouted render fails, never renders
     bytes its identity does not promise.
   - Consequence: a render whose triple no worker serves waits in `requested` until such a worker
     polls (for example an API deployed before its workers). Detecting queues without pollers is not
     built yet.

## 16. Render episodes: unroutable renders, stuck sealing with history (2026-10-05, for review)
Migration 0028, `edisc_worker.render_routing`, maintenance schedule `check-render-routing` (every
minute). Supersedes `renders.sealing_stuck_at` of §15.

1. **Episodes.** `render_episodes` holds one row per episode of a condition (`unroutable`,
   `sealing_stuck`): at most one open per render and kind (partial unique index), so exactly one
   alert (`render_unroutable`, `render_sealing_stuck`) per episode. A closed episode is history and
   never changes; its `end_reason` says why it ended (`picked_up`, `worker_available`, `sealed`,
   `render_final`). The API shows `state` (the status, or `unroutable` / `sealing_stuck` while an
   episode is open), `unroutable_since`, `sealing_stuck_since` (the current episode) and every
   episode.
2. **Unroutable.** Renders still `requested` after `EDISC_RENDER_UNROUTABLE_SECONDS` (300) are
   checked with DescribeTaskQueue on the queue of their triple. No worker polled it within
   `EDISC_RENDER_POLLER_MAX_AGE_SECONDS` (120, above Temporal's long-poll interval, so an idle live
   worker is not mistaken for a missing one): an episode opens. A worker polls again: the episode
   closes (`worker_available`); leaving `requested` closes it in `begin`'s transaction
   (`picked_up`). Losing the workers again opens a new episode with a new alert. The cross-tenant
   listing is a sweeper-owned SECURITY DEFINER function returning ids only.
3. **Stuck sealing.** The thresholds of §15 now open a `sealing_stuck` episode; the seal closes it in
   the transaction that records the seal. A seal is final and recorded once (the guard trigger
   refuses any change), so a sealed render cannot get stuck again: later seal attempts are no-ops,
   with no new episode or alert (tested). A render therefore has at most one stuck-sealing episode.

## 17. Step 5, parts A and B: corpus and crash matrix (2026-10-05, for review)
1. **Dummy connector 0.3.0.** New message kinds: `channel_leave` (the system message on odd days),
   `thread_broadcast` replies (conversation 0 forces one edited at epoch 1 and deleted at epoch 2, and
   one replying to a parent of the previous day), `me_message`, and the uninterpretable `channel_topic`
   and `pinned_item` (rendered `unknown`). Broadcasts embed their thread root (volatile, never part of
   the version). `tests/golden/dummy/small.json` regenerated deliberately.
2. **`thread_broadcast`** (approved recommendation). The normalizer already treats a broadcast as an
   ordinary message: one subject per (workspace, conversation, ts), so the copy in the history and the
   copy in the thread are the same item; `subtype` and `thread_root` are in the version fingerprint,
   and the embedded `root` is not. The renderer maps it to one `message` event with its thread
   `parent` and `custom` `slack.subtype = thread_broadcast`. The corpus proves each condition:
   counted exactly once in reconciliation, sliced by its own timestamp, rendered when its parent is
   outside the slice or out of range (context or `parent_not_rendered`), and its edit and deletion
   rendered. The legacy subtype `reply_broadcast` (older exports) also renders as a message (in the
   renderer's message subtypes since step 2; corrected in §18, which adds an older-layout case).
3. **Corpus** (`tests/integration/corpus`, `tests/integration/api/test_corpus_export.py`): 16 named
   cases collected through the real pipeline or the real export path and rendered. Every render is
   checked against an oracle computed from the dataset alone (every in-scope message exactly once;
   type, deletion, edits, reactions, attachments and placeholders per message; parents, context roots
   and `parent_not_rendered` reasons; slices by local day), the structural EML checks, `edisc-verify`
   on the package, and a golden. A coverage matrix (`oracle.COVERAGE`) must be exercised by the cases.
   Boundaries on both sides: 10,000 and 10,001 events in one slice (one file, two parts), 3 and 4 files
   at batch size 3, and 500 and 501 files at the production batch size (an acceptance run). Time zones:
   New York DST start (23 h) and end (25 h), Asia/Kolkata (+05:30), Asia/Kathmandu (+05:45).
   - Found by the corpus: a message deleted after a reaction snapshot keeps the reactions last
     observed (a tombstone has none and records no new snapshot), like its earlier text in `edits`.
     The oracle encodes this; say if a deleted message should render without reactions instead.
4. **Goldens are regression guards only.** Renderer goldens and corpus goldens are keyed by
   `golden_key()` plus `_dummy-<DummyConnector.version>` (existing generations renamed to record their
   dummy version), so a dummy bump makes a new key and never rewrites one. Corpus goldens hash the
   manifests with the tenant- and job-derived values masked (stable across runs). Recording needs
   `EDISC_RECORD_RSMF=1` / `EDISC_RECORD_CORPUS=1`, never overwrites, and refuses to run in CI.
5. **Crash matrix** (`tests/integration/renders/test_render_crash_matrix.py`): 25 points on the clean
   path (19 boundaries, 6 of them at their 1st and 2nd occurrence: planning, mid-upload, file stored,
   inside and after the batch transaction), a crash after an object is written but before its row
   completes, 4 points on the failure path and 4 on the refused path. Each resumes to the oracle's
   bytes with every event and audit once, no pending row, one object version per production and
   anchor key, a verified chain and a verified package. Plus: the anchor sweeper racing a recovering
   render on the same unanchored tail (one object and one registry row per anchor key), two identical
   requests racing on the deduplication key (one render, one set of events), and a real SIGKILL of the
   worker process during planning. A real SIGKILL during the seal is not reliably reachable (the window
   is milliseconds); the seal's sub-steps are covered by the simulated crashes.

## 18. Review decisions (2026-10-05), renderer 1.2.0
1. **Reactions recorded before a deletion are history.** A deleted event carries no RSMF `reactions`
   (which would say they exist now); each reaction last observed before the deletion goes into
   `custom` as `edisc.reactions_before_deletion` = `<name> (<count>): <users>`, like the earlier text
   in `edits`. The people who reacted stay participants. `RENDERER_VERSION` 1.2.0; new golden
   generations (renderer and corpus) under `1.2.0_unicode-15.0.0_tzdata-2026e_dummy-0.3.0`.
2. **`reply_broadcast`:** already rendered as a message (it has been in the renderer's message subtypes
   since step 2; §17.2 was wrong to say otherwise). The corpus now has an older export layout
   (`ExportOptions(legacy_layout=True)`: no blocks, no profile embeds, `reply_broadcast`, parents
   listing their replies) as the case `export_legacy_layout`.
3. **Real SIGKILLs inside the seal:** a test-only barrier (`EDISC_TEST_RENDER_BARRIER=<point>:<dir>`,
   refused by Settings outside test/ci) blocks a worker process at one seal sub-step (`seal_start`,
   `after_seal_anchor`, `seal_tx`, `sealed`); the test SIGKILLs it there and a new process finishes.
   These replace the simulated crashes of the seal on the clean path.
4. **Routing check:** one DescribeTaskQueue call per queue per run (tested).
5. **Package download zip (part C, not built):** our own deterministic STORED writer extended with
   ZIP64. No CRC32 is recorded at production write time (sealed renders are immutable, so older renders
   would need another path): CRC32 is computed while streaming and written in a data descriptor for
   every entry, one rule for all renders.

## 19. Step 5 part C: render package download (2026-10-05, for review)
Implements the part C spec agreed in the 2026-10-05 review (§18.5). Code: `edisc_custody.zipwriter`
(new, pure), `edisc_custody.render_export` (rewritten: plan + members), `edisc_custody.package_source`
(new, pure), `edisc_custody.render_package` (verifier), `edisc_api.routes.renders` (route),
`edisc_api.audit.anchor_now`. Tests: `tests/unit/custody/test_zipwriter.py`,
`tests/integration/custody/test_render_package.py`, `tests/integration/api/test_render_packages.py`.

1. **Endpoint.** `GET /v1/renders/{id}/package?outputs=reference|embed` (`export.read`: matter managers
   and tenant admins; reviewers, collectors, auditors and client admins get 403, other matters 404).
   `reference` is the default. A render without a seal gets 409 `render_not_sealed`; a sealed refused
   or failed render is packaged (its custody is what the expert checks). Response `application/zip`,
   `x-manifest-sha256`, `content-disposition: attachment; filename="render-<id>-<mode>.zip"`,
   `cache-control: no-store`, and an exact `Content-Length` (see 11 below).
2. **Format `edisc-render-package/2`.** The manifest must exist (its hash is audited) before any byte
   is sent, and the objects must not be read twice; /1 inlined anchor and seal bodies in the JSONL
   files, so its manifest hashes needed every body. /2 lists them by key, VersionId, SHA-256 and size
   (`anchors.jsonl`, `job_seal.json`) and carries the bodies as `objects/<sha256>`. The manifest has
   `sealed_at` and no `exported_at`, and records `bytes` per file. The verifier accepts /1 and /2.
   Entry order is fixed: `manifest.json`, `events.jsonl`, `files.jsonl`, `anchors.jsonl`,
   `job_seal.json`, `objects/*` by hash, `outputs/*` in render order.
3. **Two passes, one shared generator.** `plan_render_package` builds the manifest from the records:
   the render's events and `render_files` (database, in short per-page transactions), its anchors
   LISTED from S3 versions with the SHA-256 and size the registry recorded when each was written, and
   the job seal `render_started` references (registry). No object body is read, except once for an
   anchor version the registry does not hold (a shadow: an incident the verifier must see).
   `package_members` regenerates every entry from the same records, bounded by the planned head and
   file count, and checks each as it passes: the JSONL files against the manifest hashes (a
   difference raises before the next entry), every object (anchors, the seal, embedded outputs) read
   by its pinned VersionId against its recorded SHA-256 and size (more bytes than recorded raise
   before they are passed on). An embedded output whose registry row disagrees with its render record
   (state, SHA-256, VersionId) is refused. `export_render_package` writes the same members to a
   directory, so a directory export and a download are the same files with the same bytes (tested).
4. **Audit first.** `audit.render_package_read` (render, job, matter, mode, format, manifest SHA-256,
   file count, actor, request id) is committed, then the tenant's audit stream is anchored with a
   FORCED anchor and the anchor is checked to cover the event (`audit.anchor_now`), before the
   response starts. Tested at the ASGI level: the state is read when the first body byte is sent.
5. **Abort.** Any exception in the stream records `audit.render_package_aborted` (the read's fields
   plus `integrity`, the error class, the entry and detail) and re-raises, so the response never
   completes (no central directory: the partial zip is unusable). An integrity failure
   (`PackageIntegrityError`, `ZipSizeError`) also raises a `render_package_mismatch` alert. Client
   disconnects are not exceptions here and are not recorded.
6. **Zip writer** (`edisc_custody.zipwriter`, decision §18.5): STORED, 1980-01-01 timestamps, mode
   0644, UTF-8 names, no comments, the given order; a data descriptor with the CRC-32 computed while
   streaming for EVERY entry (sizes and CRC zero in the local header); ZIP64 decided from the declared
   size and position only: an entry of 0xFFFFFFFF bytes or more gets a local ZIP64 extra (zeros) and
   a 64-bit descriptor; central records get ZIP64 extras only for the fields that do not fit; a ZIP64
   end record and locator when the entry count reaches 0xFFFF or the directory size or offset does not
   fit. A member must produce exactly its declared size. The renderer keeps its own `rsmf.zip` writer:
   its bytes are fixed by `RENDERER_VERSION` (in-memory entries carry their CRC in the local header),
   so sharing the module would change goldens for nothing.
7. **`edisc-verify` reads the zip in place** (`package_source.ZipSource`): our hardened reader
   (`edisc_custody.archive`) checks every local header against the central directory, overlaps, safe
   and unique names (folded), and each entry's CRC-32 and size; nothing is extracted. A damaged entry
   is a failed check (exit 1), not an unreadable package. The verifier also refuses any file the
   manifest does not account for (outside `objects/` too) and checks every object's SHA-256 and size.
8. **Compatibility** (tests): Python `zipfile`, Info-ZIP `unzip`, 7-Zip (`7zz`/`7z`) and `ditto -x -k`
   extract a real package to the same files, which verify. macOS Archive Utility was checked by hand
   once (2026-10-05, `open -a "Archive Utility"`: byte-identical extraction, UTF-8 names included).
   ZIP64: 70,000 entries (zipfile, our reader, unzip, 7-Zip) and a 4 GiB + 1 MiB entry followed by an
   entry whose offset is past 4 GiB, streamed into a hashing sink and read back through a seekable
   synthetic source by our reader and by `zipfile` (no 4 GiB file is written; about 7 s). The tool
   tests skip locally when a tool is missing and fail in CI, which installs `p7zip-full` and `unzip`.
9. **Mutation checks:** 20 protections broken one at a time (audit before bytes, the forced anchor,
   the seal check, the permission, the abort audit and alert, object and JSONL checks while
   streaming, the output registry check, determinism, the verifier's object, manifest and
   unlisted-file checks, duplicate zip names, the descriptor flag, the CRC, each ZIP64 rule, the
   size guard); each made a test fail.
10. **Found, then fixed in the review round (11 below):** `GET /v1/renders/{id}/files/{ord}/content`
    anchored its audit only "if due", so §14.7's "anchored before any byte" did not strictly hold.

Review round (2026-10-05, after part C):

11. **Every content read is anchored before its first byte.** The routes that return evidence bytes
    are exactly three: `/v1/evidence/{id}/content` (pages, files and export archive entries; no other
    route serves export content), `/v1/renders/{id}/files/{ord}/content` and the package. All three
    now call `audit.anchor_now` (forced, checked to cover the read) after committing the read, before
    the response starts: one WORM anchor object per read. Each has a test that anchors the stream
    first (so the read lands mid-interval, where an "if due" anchor writes nothing) and reads the
    database when the first body byte is sent (`tests/integration/api/first_byte.py`).
12. **`Content-Length`.** `ZipSizer` (in `edisc_custody.zipwriter`, built from the writer's own record
    builders) gives the exact archive size from the planned entries: the manifest, the JSONL sizes it
    records, the object sizes and the outputs' recorded sizes, in member order. Tested equal to the
    streamed size in both modes and for ZIP64 archives (70,000 entries; 4 GiB + 1 MiB). An aborted
    stream ends short of it, which clients detect.
13. **`edisc-verify` is strict by default.** `--tolerate-os-metadata` accepts, in DIRECTORY packages
    only, `.DS_Store`, AppleDouble `._*`, anything under `__MACOSX/`, `Thumbs.db` and `desktop.ini`,
    and lists every tolerated file (text and `--json`). It never applies to a zip. Experts should
    verify the downloaded zip itself, not an extracted folder (a file manager may have touched it).
    The job custody package verifier (`verify_package`) does not check for unlisted files at all;
    making it strict is a separate change (backlog).
14. **What the S3 listing feeds, and divergence (decided 2026-10-05).** In pass 1, `list_versions`
    over the render's own anchor prefix (`custody-anchors/{tenant}/{render}/`, one render's anchors,
    not the bucket) decides which anchor versions `anchors.jsonl` lists, delete markers included;
    each anchor body (small) is read and hashed there. *Decision: this stays.* Anchors must survive a
    compromised database (CLAUDE.md: anchors are listed from S3, never from the DB), so the package
    holds what the bucket holds. In addition the listing is compared with the database's anchor rows
    for the render. Any difference is a divergence: `delete_marker` (a version is hidden),
    `extra_version` (a version the row does not record, e.g. a shadow), `missing_row` (an object no
    row knows), `hash_mismatch` (the row's SHA-256 or size differs from the listed bytes) and
    `missing_object` (a row whose version is not listed). The download is STILL served, so the
    expert sees exactly what the bucket holds, and the route records
    `audit.render_package_anchor_divergence` (every divergence: kind, key, version, the row's and the
    listed hash and size) and raises one `render_anchor_divergence` alert, in the transaction that
    records the read, before any byte. Each kind is tested through the API; breaking the record, the
    hash comparison, the missing-object scan or the delete-marker check each fails a test. The job
    seal is taken from the records (`render_started` + registry) and checked when streamed; a
    mismatch there aborts the download (§19.5).

## 20. Plan for §11, oversized attachments as separate natives (2026-10-05, APPROVED with changes, not built)
Refines §11 into a buildable plan. Approved 2026-10-05 with the changes in 9-13 below, which
override 1-8 where they differ. Nothing here is built.

1. **Threshold and who sets it.** Two rules, both from data the render records, never from worker
   settings (two workers with different settings must not give different bytes for one identity):
   - *Structural* (fixed in the renderer, not configurable): a part's planned `rsmf.zip` must stay
     under 4 GiB minus headroom (the manifest, EML and placeholders, computed exactly by the
     renderer's `zip_size`) and under 65,535 entries; the renderer's writer has no ZIP64.
   - *Policy* (new `RenderOptions.external_over_bytes`, default a renderer constant, proposed 1 GiB):
     any single attachment above it is always external. Set per request by whoever may create the
     render (`export.create`), bounded (1 MiB .. 4 GiB), validated like the time zone. It is part of
     `as_payload()`, so of the options hash and the render's identity. It also lets the tests force
     externals with tiny files.
   Selection per part: policy externals first; then, while the structural limits are exceeded,
   attachments leave largest first, ties by file id. Deterministic from the inputs and options.
2. **How the RSMF references the native.** As §11: the event's attachment becomes the placeholder
   `{file_id}_EXTERNAL.txt` (`display` = original name; text: name, size, SHA-256, reason
   `exceeds_rsmf_zip_limit` or `over_external_threshold`, and the package path `natives/<sha256>`);
   `custom` gains `edisc.file_external = <file id>: sha256:<hex>`. The native's bytes never pass
   through the renderer: the opener is not called for an external file (tested).
3. **Storage and dedup.** One object per (render, SHA-256): `t/{tenant}/productions/{render}/natives/
   sha256/<hex>`, streamed from the pinned evidence version, hashed on read and on write, locked
   under the matter, `evidence_objects` kind `production` with `render_id` (retention render -> job ->
   matter, as for `.rsmf` files). The same file referenced by several events, parts or slices is
   written once. A retry finds the registry row and the object (idempotent, like the evidence writer).
   *Open question:* §11 copies the native. Referencing the collected file evidence (already WORM,
   pinned, same matter) avoids a second copy of multi-GB files; the cost is that the production is
   no longer self-contained under its own key. Recommendation: keep the copy (§11), decided once.
4. **Custody.** New insert-only `render_natives` (render, ord, SHA-256, size, key, VersionId, the
   file ords that reference it), tied to its batch event by a deferred FK like `render_files`. A
   native is written to WORM before the batch that first references it; the batch transaction then
   inserts its native records and the file records together, and `render_files_batch` gains the
   root over that batch's native records (`natives_root`; leaf = canonical JSON of the record).
   `render_completed` gains the native count and the root over all native records; the summary
   gains `external_attachments`. The reconciler fails if an external reference has no native with
   the recorded SHA-256 and size. (One event per native, as §11 said, is replaced by the batch root:
   bounded events whatever the number of natives.)
5. **Byte identity and dedupe keys.** Same inputs + same options (now including the threshold) +
   same renderer, Unicode and tzdata versions -> same bytes, natives included (content-addressed).
   The change alters output bytes where it applies and the options payload everywhere, so it ships
   as `RENDERER_VERSION` 1.3.0 with new golden generations (renderer and corpus); the render identity
   index already includes the options hash, so old renders are untouched and a new request renders
   anew. `FILE_FIELDS` gain `external_count`; the verifier keeps accepting the old leaf for renders
   made before 1.3.0 (decided by the renderer version in `render_started`).
6. **Package layout** (`edisc-render-package/3`; the verifier accepts /1, /2, /3): `natives.jsonl`
   (one record per native, hashed in the manifest), `natives/<sha256>` when `outputs=embed` (after
   `outputs/`, by hash), or referenced by hash and supplied with `--file` like the outputs. The
   verifier checks every native's SHA-256 and size, the `natives_root` of each batch and the total,
   and opens each `.rsmf` with the hardened reader to check that every `edisc.file_external` it
   carries has a native record (and that no native is unreferenced). `Content-Length` and ZIP64
   cover natives (a package over 4 GiB is now normal).
7. **API.** `GET /v1/renders/{id}/natives` (list, `custody.read`) and
   `GET /v1/renders/{id}/natives/{sha256}/content` (`export.read`, audited and `anchor_now` before
   the first byte, re-hashed while streaming, first-byte test).
8. **Tests.** Pure renderer: selection (policy, structural by size and by entry count, exactly at the
   limit stays, one byte over leaves, ties by file id, a single attachment over 4 GiB), placeholder
   and `custom` bytes, the opener never called for externals, byte identity, goldens 1.3.0.
   Reconciler: missing native, wrong hash, unreferenced native. Corpus: new cases (external by
   policy with a tiny threshold, the same file in two slices written once, external next to an
   unavailable file, an external in a thread root shown as context). Crash matrix: crash after the
   native PUT before its row completes, after the native rows before the batch commits, and a real
   SIGKILL while a native streams; each resumes with one object version per native key and the
   oracle's bytes. Package: embedded and referenced natives, altered/missing/unreferenced native,
   an `.rsmf` referencing an unlisted native, `Content-Length` with natives, ZipSizer past 4 GiB.
   Retention: natives resolve to the matter and are extended by the extension job. API: permissions,
   first-byte audit, mismatch abort. A real multi-GB native is a measurement for the cloud VM
   (backlog), not a laptop test.

Review decisions (2026-10-05), overriding the plan above where they differ:

9. **Natives are copied into the production** (the open question in 3 is closed): collected files
   belong to the client, productions to the matter, and a matter-owned production must not depend on
   a client-owned object. The copy is SERVER-SIDE: `CreateMultipartUpload` on the native key (Object
   Lock COMPLIANCE and retain-until set at create, as for every production), then `UploadPartCopy`
   with `CopySourceVersionId` = the pinned evidence version, in fixed part ranges, then
   `CompleteMultipartUpload`; any failure aborts the upload. Then ONE streaming SHA-256 read of the
   destination's pinned VersionId must equal the source's recorded SHA-256 and size before the
   native is recorded complete. Native bytes never pass through the worker except that one
   verification read (never twice). MinIO ignores If-None-Match on copies, so the write is serialized
   by the advisory content lock, like `EvidenceWriter`; a key already complete in the registry is
   reused (retry). The native's evidence row stores our SHA-256, never S3's composite checksum.
10. **Externalization handles BYTES only.** The entry count never externalizes a file. A part whose
    zip would exceed 65,535 entries is split instead, by this rule: the zip of a part holds a fixed
    set of entries (the manifest and the EML, `F` = 2 today) plus one entry per attachment or
    placeholder of its events, primaries and context. After the existing event-cap split, walk each
    part's events in render order and close the part before the first event whose entries would bring
    the total over 65,535 - `F`; the context roots a part needs are recomputed for each resulting
    part exactly as `split_parts` does today (a reply's root goes with it as context). Parts are then
    renumbered `part`/`parts` over the slice. The rule depends only on the ordered events and their
    attachment counts, so it is deterministic. A single event whose own entries cannot fit raises
    `RenderInputError` (impossible for Slack, which caps files per message).
11. **The 4 GiB check is exact.** The renderer moves to `edisc_custody.zipwriter` in this same
    version bump (the BACKLOG item "unify the renderer's zip writer" is done by §11: the bump that
    changes the bytes anyway). The structural rule is "the part's zip needs no ZIP64": computed with
    `ZipSizer` (the same code that gives the package's `Content-Length`; to gain a `needs_zip64()`
    answer: any entry of 0xFFFFFFFF bytes or more, any offset, directory size or directory end at
    0xFFFFFFFF or more, or 65,535 entries or more). No guessed headroom. Externalization: policy
    externals first, then largest first (ties by file id) until `needs_zip64()` is false.
12. **Placeholder bytes are fully pinned.** `{file_id}_EXTERNAL.txt` (and, unchanged,
    `_UNAVAILABLE.txt`): UTF-8 without BOM, LF line endings, one `field: value` line per field in a
    fixed order (`name`, `size`, `sha256`, `reason`, `native`), the name NFC-normalized, a final LF,
    no timestamps, no locale-dependent formatting (sizes as plain decimal integers). Covered by the
    renderer goldens (whole `rsmf.zip` bytes) and by a unit test on the exact bytes.
13. **Backlog:** cross-render native dedupe (one native object per matter and SHA-256 shared by
    renders), after measuring the storage and copy cost on the cloud VM.

