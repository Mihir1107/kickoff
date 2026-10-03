# ADR 0015: RSMF renderer (`edisc_renderers.rsmf`)

Status: **Proposed** (2026-10-03), for review before any code (M15). Implements M15 of
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
- **Scope:** every in-scope message of the job. Out-of-scope items appear only as thread context
  (§3), marked.
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
  - `type`: `direct` for DMs and group DMs, `channel` for public and private channels.
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
  - **Thread context** (ADR 0011): a reply in this file whose root sits in an earlier slice or part is
    preceded by its root. The root carries `custom` `edisc.context = thread_root_outside_file`, so
    every `parent` resolves inside the file.
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
  - `X-RSMF-Version: 2.0.0`; `X-RSMF-Generator: edisc-renderers/<version>`;
  - `X-RSMF-BeginDate` / `X-RSMF-EndDate`: the first and last event timestamps;
  - `X-RSMF-EventCount`; `X-RSMF-AttachmentCount`; `X-RSMF-Application: Slack`;
  - `X-RSMF-Custodian` (when mapped); `X-RSMF-Participants` (display names, folded per RFC 5322);
  - `X-RSMF-EventCollectionID`.
- **Custom headers:**
  - `X-RSMF-CollectionId` (job id) and `X-RSMF-ConnectorVersion` / `X-RSMF-NormalizerVersion`.
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
- deflate at a fixed level, no extra fields, no comments;
- canonical JSON and derived boundary and ids.

A golden test compares bytes (`tests/golden/rsmf/`). Any change to them needs a renderer version bump,
recorded in the render.

### 7. Storage, custody, API
- **Storage:** renders are derived products, never evidence. They go to
  `t/{tenant}/productions/{render}/...`, hashed while written and locked for the matter window, as
  registry rows of a new kind `production`.
- **Custody:** a job's own chain is sealed when it finishes, so a render gets its **own stream**
  (stream id = render id). That stream holds `render_started`, then one `rsmf_rendered` event per file
  (name, slice, part, SHA-256, size, source hash, event count), then a seal, anchored like a job. The
  tenant audit stream records who asked for it (`audit.render_requested`) and the render's final head.
- **API:**
  - `POST /v1/jobs/{id}/renders` (`export.create`: matter_manager, tenant_admin; recent sign-in per
    ADR 0016). The body has the time zone and options. It runs as a Temporal `RenderWorkflow` (queue
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

## Open points (for review)
1. A render's custody on its own stream (proposed above), or an exception that lets a sealed job stream
   accept render events. The proposal keeps job seals final.
2. `type` for group DMs: `direct` (proposed) or `channel`.
3. Whether out-of-scope messages other than thread context are ever rendered. Proposed: never.
