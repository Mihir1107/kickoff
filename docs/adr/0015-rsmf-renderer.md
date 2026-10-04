# ADR 0015: RSMF renderer (`edisc_renderers.rsmf`)

Status: **Accepted** (2026-10-03) with the review decisions below (§9). Steps 1–3 implemented and
approved (§10, §12 with the 2026-10-04 decisions in §13); steps 4–5 not yet (M15). Implements M15 of
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
  - Then one `rsmf_rendered` event per file: name, slice, part, SHA-256, size, source hash, event count,
    context event count.
  - Then `render_finished` and a seal, anchored to WORM like a job chain.
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
