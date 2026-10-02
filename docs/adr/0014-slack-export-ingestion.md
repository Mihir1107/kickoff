# ADR 0014: Slack export ingestion (`edisc_connector_slack_export`)

Status: **Accepted** (2026-10-02) with review changes R1–R7 (below, and folded into the sections). Follows the phase-2 decisions in
`docs/plans/phase-2.md` (1: entries referenced inside the locked zip; 2: `matched_against_archive`;
3: real exports become fixtures).

## Context
An admin downloads Slack's standard export (a zip) and uploads it to us. The zip is the evidence. Its
entries then flow through the existing pipeline (normalizer, exactly-once batches, custody, reconciliation).

Slack's documented format:
- top-level `users.json` and `channels.json`;
- on some plans and exports also `groups.json` (private channels), `dms.json` and `mpims.json` (group DMs),
  plus `integration_logs.json` and other optional files;
- one folder per conversation with one `YYYY-MM-DD.json` file per day: a JSON array of message objects
  shaped like `conversations.history`.

Until real exports arrive, everything is built against that documented format and a **synthetic export
generated from the dummy oracle**. The real exports (a Developer Program sandbox and a free-plan workspace)
become fixtures when provided, and every format assumption below marked *(confirm on real export)* is
checked against them.

## Decision

### 1. Upload: the zip is hashed and locked before anything reads it
- **Session API:**
  - `POST /v1/clients/{c}/exports` (`connection.manage`, decision a: client-owned) opens an upload
    session. The body declares the size and, optionally, the client's SHA-256 and the plan the admin
    believes the workspace is on.
  - The client uploads parts with `PUT /v1/exports/{id}/parts/{n}` (≥ 8 MiB except the last, each with a
    `Content-Digest: sha-256=…` header that we verify per part), then calls
    `POST /v1/exports/{id}/complete`.
  - Parts go into an S3 multipart upload in the **staging** bucket under `exports/{tenant}/{export}`.
    The session lives in `slack_exports`, and the parts (number, size, our SHA-256, ETag) in
    `export_upload_parts`, so uploads resume across client and API restarts. A part may be re-sent until
    completion. Nothing touches local disk; the API holds one part (≤ 64 MiB) per request in memory.
  - Staged exports live under their own lifecycle rule (`exports/`, 7 days, `EDISC_EXPORT_UPLOAD_TTL_DAYS`),
    since a large upload can take more than the staging bucket's usual day.
  - Completion checks the parts (numbered 1..n, each but the last ≥ the minimum, sum = declared size) and
    that the store still holds exactly the recorded parts; then a Temporal workflow (`exports` queue)
    hashes, locks and validates. `complete` answers 202 with the export, waiting briefly
    (`EDISC_EXPORT_COMPLETE_WAIT_SECONDS`) for the lock step so a declared-hash mismatch comes back as
    the 422 directly; if hashing takes longer, a later `complete` or `GET` reports it.
- **On complete,** the existing large-file evidence path (ADR 0002) runs, unchanged:
  1. one streaming pass over the staged object computes **our** SHA-256 and size (bounded memory);
  2. the result must match the declared size;
  - **R7:** if the client declared a SHA-256 and it differs from ours, the export is **rejected before any
    parsing**. It is still locked as evidence (what we received is what we keep), its status is
    `rejected` with reason `declared_hash_mismatch`, the mismatch is audited, and the client gets a 422
    with both hashes. This is an error, never a warning.
  3. the source hash is persisted on a pending registry row;
  4. a server-side copy goes into the content-addressed locked key, the destination is verified, the
     version is pinned, and staging is deleted.
- **Duplicates:** an identical re-upload is a dedup hit (same tenant).
- **Record before processing:** an `audit.export_uploaded` event (uploader, size, SHA-256, evidence id,
  VersionId, declared plan) is committed before any processing. The zip's SHA-256 is returned to the
  uploader so they can compare it with their own copy.
- **The export becomes a source:** an export is a credential-less `connections` row with
  `source = 'slack_export'` and `config = {export_id}`, owned by the client. Jobs reference it like any
  connection, so scopes, `job.start`, multi-scope and custody all work unchanged.
- **Validation before a job can use it:** after upload, a quick structural pass (central directory only,
  §3) validates the archive and **detects its tier** (§5). The tier and the archive-level findings are
  stored on the export and shown before a job is started.

### 2. Entry references, verifiable offline
- **Registry rows:** each zip entry a job reads gets an `evidence_objects` row of the new kind
  `archive_entry`:
  - `archive_evidence_id` (the zip), `version_id` (the zip's pinned VersionId) and `entry_path` (exact
    name bytes from the central directory, UTF-8 decoded per the zip flag);
  - `entry_crc32` and `entry_compressed_size` from the central directory;
  - `sha256` and `size_bytes` of the **decompressed** entry bytes;
  - `storage_key = "{zip storage_key}#{entry_path}"` keeps the unique key.
  - The decompressed hash and CRC-32 are written before any item references the row (same provenance
    rule as pages).
- **Items:** they point at the entry row exactly as they point at pages today: `evidence_object_id`,
  `json_path` into the entry's JSON array, and `raw_hash` = SHA-256 of the canonical JSON of that
  fragment. The normalizer is unchanged apart from the export dialect (§7).
- **Offline verification, package format `edisc-custody-package/2`.** The verifier accepts /1 and /2.
  - `evidence.jsonl` carries the archive fields.
  - With objects included, `objects/` holds the zip itself (once, by SHA-256).
  - For each `archive_entry`, `edisc-verify`:
    1. checks the zip's SHA-256;
    2. finds the entry by exact path in the central directory, rejecting duplicates;
    3. checks the CRC-32 recorded at collection against the central directory;
    4. decompresses under the same limits (§3) and checks the CRC-32 and the recorded decompressed
       SHA-256 and size;
    5. then checks every item fragment hash as for pages.
  - The zip reader used by the verifier is a **pure module** (`edisc_custody.archive`, stdlib `zlib`
    only, no S3 or DB imports; the existing import-isolation test covers it). The worker uses the same
    reader over an S3 range-read adapter, so collection and verification parse archives identically.

### 3. Hostile and broken archives: explicit limits, all configurable
The archive is attacker-influenced, since anyone with export access can craft one. Declared sizes are
never trusted: every limit is enforced on bytes actually read or produced. All limits are `EDISC_EXPORT_*`
settings. **R1:** a tenant admin may override any limit for one upload (in the upload session); every
override is recorded in the audit chain with the old and new value. The central directory is parsed **as
a stream** with bounded memory: entries are validated and written to the database in batches, never held
as one list (a test parses a synthetic directory of several million entries and checks peak memory).

| Check | Default limit / rule | On violation |
|---|---|---|
| Zip size | 200 GB | upload refused (413) |
| Entries in the central directory | 20,000,000 (R1) | archive rejected |
| Decompressed size per entry | 1 GiB | archive rejected |
| Total decompressed size | min(100 × compressed size, 2 TB) | archive rejected |
| Compression ratio per entry | 200:1 for entries over 1 MiB decompressed | archive rejected |
| Compression methods | stored (0) and deflate (8) only | archive rejected |
| Encrypted entries, multi-disk archives | not allowed | archive rejected |
| ZIP64 | supported; every ZIP64 field cross-checked against the end-of-central-directory records and the object size | archive rejected on mismatch |
| Entry names | UTF-8; no NUL, no backslash, no absolute path, no drive letter, no `..` segment, no empty segment, ≤ 1,024 bytes, no symlink attributes | archive rejected |
| Duplicate names | exact or case-folded (NFC) duplicates | archive rejected |
| Allowed layout | top-level `*.json`, `<folder>/<YYYY-MM-DD>.json`, plus known optional top-level folders | others are listed as **unknown entries** in the report: kept in the evidence, not processed, never silently dropped |
| Truncation / corruption | the end-of-central-directory record must be found within the last 64 KiB + comment; every central-directory offset and size must lie inside the object; each local header must match its central-directory entry (name, method, sizes, CRC); trailing or overlapping data is flagged | archive rejected at validation; if found during a job, a **job-scoped integrity failure** (ADR 0012) |
| CRC-32 | checked on every entry read | job-scoped integrity failure |
| Decompression | streaming `zlib.decompressobj` in bounded chunks; stops one byte past the declared size or the limit, whichever is first | integrity failure |

**R5 (fuzzing):** the zip reader is fuzzed with property-based tests (hypothesis) over mutated, truncated
and spliced archives. The invariant is: every input either parses within limits or is rejected with a
classified `ArchiveError`, never a crash, hang, unbounded memory use or an escape of a limit. CI runs a
short budget; a longer budget is documented for manual runs.

**R6 (S3 access):** entries are read in **local-header order** with large sequential range reads (several
MiB, adjacent entries coalesced into one request), not one request per entry. Requests per 1,000 entries
are measured on the synthetic export and recorded in `docs/runs/`.

"Archive rejected" means:
- the export stays as locked evidence;
- its status becomes `rejected`, with the finding recorded in the audit chain;
- no job can use it.

Tests cover each row with crafted zips, plus a 5 GB streaming archive with bounded memory and the
SIGKILL crash matrix during ingestion.

### 4. Reconciliation: `matched_against_archive`
An export has no server-side counts. Completeness is checked against the archive itself.
- **Unit = one day file** (`<folder>/<YYYY-MM-DD>.json`): it is what is fetched, checkpointed and
  reconciled. A conversation is the folder named in `channels.json`, `groups.json`, `mpims.json` or
  `dms.json` (for DMs the folder name is the DM id) *(confirm on real export)*.
- **R4 (dates):** every message is assigned to its conversation-day from its **own `ts`** (UTC), never
  from the file name. The file name's date is recorded only as a hint; a message whose `ts` falls outside
  the hinted day is reported as an anomaly. The time zone of the file names is *(confirm on real export)*
  (believed to be the exporting workspace's time zone). Consequences:
  - a day file is enumerated for a scope if the hinted day ±1 day overlaps the scope's range;
  - each message's `in_scope` comes from its `ts`, as today.
- **Per unit,** every element of the day file's JSON array must be accounted for exactly once: it becomes
  a message item, an event item (join, leave, topic, bot) or a recorded, typed skip. The file must parse.
  The unit's status is then `matched_against_archive`; otherwise `gap`, with the unaccounted indexes
  listed.
- **Per archive** (job-level, in the report):
  - every central-directory entry is processed, out of scope, or listed as unknown;
  - every conversation in the metadata files has a folder or is reported as "no messages in export";
  - every folder maps to a listed conversation or is reported as unknown;
  - observed first and last day per conversation.
- **Job status:**
  - a new terminal status `completed_against_archive` when every unit is `matched_against_archive`;
  - existing statuses when there are gaps, failed units or unverifiable units.
  - It is **never** `completed`: the API's `clean` is false, with `clean_basis: "archive"`.
  - ADR 0005 is amended.
- **Report caveat** (verbatim, in the report and the API):
  > "Completeness was verified against the provided Slack export only. Every entry of the export was
  > accounted for, but the export's own completeness relative to the Slack workspace was NOT verified:
  > content excluded by the plan, the export's date range, Slack retention settings or the export
  > settings cannot be detected from the export."

### 5. Export tier detection and blind spots
- **Detected from the archive alone**, recorded with the export, shown before a job and in the report:
  - `public_only`: `channels.json` present; no `groups.json`, `dms.json` or `mpims.json`. This is the
    standard export on Free and Pro, and on Business+ without an approved full export.
  - `full`: any of `groups.json`, `dms.json`, `mpims.json` present (Business+ or Enterprise Grid full
    export).
  - `grid`: Enterprise Grid org-level structure. Detection markers *(confirm on real export)*: org-level
    user files and per-workspace layout. Until confirmed, a Grid-looking archive is processed as `full`
    and flagged "tier unconfirmed".
  - If the admin declared a plan at upload, declared vs detected is reported. A mismatch (e.g. declared
    Business+ but `public_only`) is a warning: an export without private data is a legitimate choice, but
    it must be visible.
- **Blind spots stated per tier:**
  - `public_only`: private channels, DMs and group DMs are not in the export.
  - All tiers:
    - edits keep only the latest text (earlier versions are not in the export);
    - deleted messages are absent (no tombstones);
    - message history may be limited by the plan's visible history or the workspace retention
      settings, and the export's date range is chosen by whoever ran it;
    - file contents are not in the zip: only links, downloaded separately (§6);
    - canvases, lists, huddle transcripts and Clips are absent unless their files are present
      *(confirm on real export)*;
    - shared-channel content from other organisations may be partial.
  - The free-plan workspace export and the sandbox export are the test cases for `public_only` and
    `full` respectively.

### 6. File URLs inside the export are secrets
- **The risk:** message `files[]` entries carry `url_private` / `url_private_download`. In exports these
  carry an access token (query parameter `t=`) *(confirm on real export)*.
- **Never stored outside the evidence:**
  - The zip itself is evidence and stays byte-exact, so the tokens live only inside locked evidence.
    Reads of it are audited content reads (ADR 0013 decision d).
  - **Derived data never contains them:** the normalizer stores file URLs with the query string removed,
    and file identity is the Slack file id.
  - **Logs never contain them:** every token value found while parsing is registered with
    `register_secret`, and the redaction patterns already cover `xox*` tokens.
  - **Temporal never carries them:** activities pass the file id and entry reference; the URL is re-read
    from the entry inside the activity.
- **Downloads:**
  - They go through the rate limiter (bucket `slack_export.file`, limits from `EDISC_RATE_LIMITS`).
  - They use the existing small/large file evidence paths.
  - A failed or expired URL is a recorded file gap, not a job failure, as today.
- **The report warns** that the uploaded export contains file-access tokens, and that the workspace admin
  should rotate or revoke export links per Slack's guidance once collection is complete.

### 7. Connector and normalizer
- **The connector stays thin:** it enumerates units from the metadata files and folders, then yields each
  day file's decompressed bytes as a `RawBatch` with its entry reference. The cursor is the entry path,
  so resume is exact.
- **Directory unit:** `users.json` becomes identity snapshots.
- **The Slack normalizer gets a `slack_export` dialect:**
  - embedded `user_profile`, file stubs, channel events (`channel_join`, `channel_leave`, topic,
    purpose), bot and app messages;
  - fingerprints stay the same where the meaning is identical to the API (so an export and a live
    collection of the same message dedupe), and get a new fingerprint version where not.
- **The dummy generator gets a `slack_export` dialect** that writes a real zip from the oracle (public-only
  and full variants, including DMs and group DMs), so oracle-exact tests run on export data.

### Implementation notes (M14.3)
- **Tables (migration 0018):** `slack_exports` (status `uploading → locking → validating → ready |
  rejected`; `locking → uploading` only before anything is locked, when the store refused the parts),
  `export_upload_parts`, `export_entries` (the central directory, one row per entry) and
  `export_conversations`. A trigger keeps the declared columns and the locked archive immutable and the
  final states final; the directory tables are append-only.
- **Duplicate names at 20M entries:** `export_entries` has a unique `(export_id, folded_name)`; the
  validation pass inserts in batches and a violation is the `duplicate_name` rejection. Nothing holds
  the directory in memory.
- **Overlap at validation** is a lower bound (local header ≥ previous header start + 30 + compressed
  size, checked in SQL over the offsets). The exact check (with names and extra fields) runs on every
  read through `open_entry(data_end_limit=...)`.
- **Metadata files** are streamed element by element (`edisc_core.jsonstream`, element cap
  `EDISC_EXPORT_MAX_JSON_ELEMENT_BYTES`). An unparseable conversation metadata file rejects the archive
  (`metadata_invalid`); an archive without any conversation metadata file is `not_a_slack_export`.
- **Retention:** the export is not tied to a matter when uploaded, so it gets the rolling window. Jobs
  that use it must extend it like any dedup hit (M14.5).
- **Audit events (tenant stream):** `export_upload_started` and `export_limits_overridden` (uploader),
  `export_upload_completed` (uploader), `export_uploaded`, `export_rejected` / `export_validated` and
  `export_upload_reopened` (actor `system:export-ingest`, uploader named in the payload).

## Consequences
- Plus: the uploaded bytes are locked and hashed before any parsing. Every item traces to an entry
  verifiable offline from the zip alone, with no second copy of the data.
- Plus: hostile archives are rejected on measured bytes with stated limits, and nothing is silently skipped.
- Plus: the export's limits (tier, plan, date range, retention) are stated, never implied away. Archive
  completeness is never shown as source completeness.
- Minus: offline verification needs the whole zip in the package, even for a small job scope.
- Minus: Grid detection and several format details stay *(confirm on real export)* until the fixtures
  arrive. The defaults are conservative: process as `full`, flag the tier as unconfirmed.

## Review changes (2026-10-02)
- **R1:** up to 20,000,000 entries; per-upload limit overrides by tenant admins, audited; streaming
  central-directory parse with bounded memory, tested at several million entries.
- **R2:** unknown entries: the export is processed and they are listed in the report.
- **R3:** status `completed_against_archive`.
- **R4:** dates from each message's `ts`; the file-name date is a hint, and a mismatch is an anomaly.
- **R5:** property-based fuzzing of the zip reader.
- **R6:** sequential, coalesced range reads in local-header order, measured.
- **R7:** a declared-hash mismatch rejects the export before parsing.
