# Phase 2 plan: Slack export ingestion, RSMF, preview and report, minimal UI (proposal)

Status: **proposed, for review.** Nothing in this document is implemented. Each milestone runs
implement → tests → run → commit, starting with an ADR where marked.

**Prerequisite (recommended first, small):** anchor coalescing (docs/runs/2026-10-01-audit-burst.md, option A).
- Every phase-2 milestone adds custody and audit events: render, report and download.
- The current anchor storm writes one WORM anchor per ~2 events under concurrency instead of one per 8.
- It should be fixed before volume grows.

---

## M14: Slack export ingestion connector (`edisc_connector_slack_export`)
An admin uploads the standard Slack export zip. The zip is evidence first, and its entries then flow
through the existing pipeline.

**ADR 0014 (before code)** covers:

**1. Upload and lock first.**
- `POST /v1/clients/{c}/exports` takes a resumable chunked upload, so multi-GB zips work through proxies.
- The bytes stream straight into the existing evidence writer: hashed while streaming, Object Lock set at
  creation, nothing on worker disk.
- The zip's SHA-256 and size are returned and recorded in an `audit.export_uploaded` event before any
  processing.
- An "export" is a connection-like source owned by the client (decision a). A job references it like a
  connection.

**2. Reading the zip from WORM without local disk.**
- A seekable reader over the pinned object version uses S3 range GETs (the central directory is at the end).
- Each entry is streamed and its CRC-32 is checked against the central directory.
- The entry's own SHA-256 is computed while it is read.
- A corrupt or truncated archive is a job-scoped integrity failure.

**3. Provenance of entries.** Proposed: no second copy.
- Items point at a new evidence kind `archive_entry`: the zip's evidence id, the entry path and the entry
  SHA-256, plus `json_path` inside the entry.
- `edisc-verify` re-reads the entry from the pinned zip and checks its hash.
- Alternative: write every day file as its own page object. That is simpler for the verifier but doubles
  storage. **Decision needed.**

**4. Units and reconciliation.**
- Units are `channels.json`, `groups.json` (private channels), `dms.json` and `mpims.json` × the
  per-day files (`<channel>/<YYYY-MM-DD>.json`), filtered by the job's scopes.
- An export has no server-side counts, so the source's expected count is unavailable. Units would end
  `completed_unverified` (ADR 0005), which is correct but not useful.
- Proposed **archive completeness** instead:
  - every central-directory entry is accounted for (processed, out of scope, or listed as unknown);
  - every listed channel has its directory;
  - every day file parses;
  - per-file message count = items produced.
- A unit is then `matched` against the export's own content, and the report states that the export is
  the system of record ("verified against archive", not against Slack). **Decision needed:** new recon
  status `archive_matched`, or reuse `matched` with a report note.

**5. Business+ / Enterprise exports.**
- Private channels (`groups.json`), DMs (`dms.json`) and group DMs (`mpims.json`).
- Shared channels and external users.
- `users.json` becomes identity snapshots (the existing directory unit path).
- Deleted users, bot and app messages, channel join/leave/topic events, edited and deleted messages, and
  threads across days are handled.
- Canvases, lists and huddle transcripts are listed as blind spots unless present.

**6. Files.**
- Export messages carry `url_private_download`; on Business+ exports these include a token parameter.
- These URLs are **secrets**: registered for redaction, never stored in derived data, never logged.
- Downloads go through the rate limiter (`slack_export.file` bucket) and the existing small/large file paths.
- Missing or expired files are recorded as file gaps, as today.

**7. Normalizer.**
- Export messages are close to `conversations.history`. A `slack_export` dialect is added to the dummy
  generator so the oracle tests run on export-shaped data too.
- The Slack normalizer gets export-specific parsing (embedded `user_profile`, file stubs, channel events)
  behind the same fingerprint versions where the meaning is the same. A new fingerprint version is used
  where it is not.

**Tests:**
- Oracle-exact on dummy export zips.
- Corrupt or truncated zips, a CRC mismatch, entries outside any scope, path traversal entries (`../`),
  zip bombs (ratio and size caps), duplicate entry names, and 5 GB streaming with bounded memory.
- SIGKILL during ingestion resumes exactly (the existing crash matrix).
- **A real export from a Slack Developer Program sandbox:**
  - Business+/Enterprise features: private channels, DMs, threads, edits, deletions, files.
  - The sanitised export is stored as a CI fixture, or in a private bucket if too large, plus a
    hand-checked expected summary (counts per channel-day, thread structure).
  - **Needs from you:** a sandbox workspace and an exported zip. I cannot create the Slack account
    myself.

---

## M15: RSMF renderer (`edisc_renderers.rsmf`)

**ADR 0015** covers:

**1. Input and slicing.**
- Input: derived items (latest normalizer version) plus file evidence, never raw pages directly.
- Slices are one conversation per 24 h by default. UTC by default; a matter time zone is optional and uses
  `local_day_bounds`, including DST.
- Cap: 10,000 events per file; above that the slice is split into `part N of M`.
- Threads stay readable: a reply in a slice whose parent is in an earlier slice carries the parent as
  context (marked), following ADR 0011.

**2. Output.**
- An `.eml` with `X-RSMF-Version` set to the pinned version, `X-RSMF-Generator`, `X-RSMF-BeginDate`,
  `X-RSMF-EndDate`, `X-RSMF-EventCount`.
- Custom headers:
  - `X-RSMF-CollectionId` (job id);
  - `X-RSMF-SourceHash`: Merkle root over (idempotency key, content hash) of the slice's items, using the
    same RFC 6962 construction as custody, so it can be checked against the chain;
  - `X-RSMF-ConnectorVersion` and `X-RSMF-NormalizerVersion`.
- The attachment `rsmf.zip` contains `rsmf_manifest.json` (participants, conversations, events with
  parent/thread, reactions, edits, deletions, attachments by reference), the attachment files and,
  optionally, avatars.
- Unavailable files appear as placeholder attachments with the reason.

**3. Determinism.** The same inputs give byte-identical output: fixed zip timestamps and ordering, and
canonical JSON where the schema allows. A golden test compares the bytes.

**4. Storage and custody.**
- Renders are derived products, not raw evidence: `t/{tenant}/productions/{job}/...`.
- They are hashed while written and locked for the matter window.
- Each render is a `report_generated`-style lifecycle custody event (`rsmf_rendered`) listing every file
  with its hash.

**5. Validation in CI.**
- Manifests are validated against Relativity's published RSMF JSON schema, pinned and vendored with its
  version and source URL. The EML is checked structurally.
- **Proposed:** also run Relativity's RSMF validator in CI if its licence allows it (it is a .NET tool;
  the CI job would add the .NET runtime). **Decision needed.**
- Fixture corpus: edge cases (emoji/RTL/zero-width, very long messages, attachment-only, deleted with
  tombstones, edits, cross-day threads, the 10k split) rendered and validated.

**API:**
- `POST /v1/jobs/{id}/renders` (permission `export.create`: matter_manager or reviewer?
  **Decision needed**).
- `GET .../renders/{id}`, and downloads through the audited content path (purpose `rsmf`).

---

## M16: HTML preview and the collection report

**1. Preview.**
- Renders from the same intermediate as RSMF: a conversation-day view with threads, edits, deletions,
  reactions and attachments.
- Static, sanitised HTML: everything escaped, no scripts, no external resources, strict CSP.
- Attachments link to the audited content endpoint (purpose `preview`). Served by the API to permitted
  roles only.

**2. Collection report** (HTML + JSON, plus PDF?, **decision needed**), deterministic and hashed. Contents:
- scopes (ranges, policies) and the access tier, plan and granted scopes;
- **blind spots** (from `validate_connection` and per-source lists);
- counts per unit;
- gaps, unverifiable and failed units, with reasons;
- file unavailability;
- access-lost observations and no-longer-observed events;
- pauses (who re-authorized, how long);
- connector and normalizer versions;
- operator/actor for every action;
- the **custody verification result** (events, batches, items, anchors and Merkle roots checked, seal);
- the evidence store's lock settings.

**3. Reporting rules.**
- `completed_unverified` and `completed_with_failed_units` are never rendered as success.
- The report is a lifecycle custody event `report_generated`. The report's hash is included, so a later
  change to the report is detectable.

---

## M17: minimal UI (Next.js)
Pages: connections (list, create, re-authorize; export upload), create collection (matter, connection,
scopes form with ranges and policies), job status (live; units; pauses; cancel/resume/rerun), report
view, downloads (RSMF and report, through the audited endpoints).

**ADR 0016 (before code)** covers:

**1. Auth model.** Proposed: a **BFF** (backend-for-frontend).
- The Next.js server does OIDC (authorization code + PKCE) with the tenant's IdP and keeps tokens
  server-side in an encrypted, httpOnly, SameSite=strict session. The browser never sees a token.
- The alternative is a pure SPA holding tokens in memory: simpler, weaker. **Decision needed.**

**2. Tenant from the host.** The same subdomain scheme as the API, so the UI can't pick a tenant either.

**3. Security.**
- Strict CSP, no inline scripts and CSRF protection on BFF mutations.
- Downloads stream through the BFF and keep the audit purpose.

**4. Testing.** Playwright end to end against the live stack: dummy connection → collection → status →
report → download, with the audit trail checked. Plus accessibility checks (axe).

Out of scope for M17 (backlog): admin screens for IdPs and roles (API-only for now), theming, i18n.

---

## Decisions (2026-10-02)
1. **Export entries:** referenced inside the locked zip by (zip evidence id, pinned VersionId, entry path).
   - The SHA-256 of the **decompressed** entry bytes is recorded, plus the zip's CRC-32 for the entry.
   - `edisc-verify` must extract and verify entries from the zip offline. The custody package therefore
     carries the zip (or the referenced zip objects).
2. **Reconciliation:** a new status `matched_against_archive`.
   - The report must state that completeness is relative to the provided export, and that the export's
     own completeness against the source is NOT verified.
   - It is never presented as equivalent to a live `matched`; it is not "clean" in the API's `clean` flag.
3. **Real exports:** you will provide a Developer Program sandbox export and a free-plan workspace export.
   Until then, build against the format docs and the dummy (`slack_export` dialect). Both become fixtures
   when they arrive.
4. **Relativity RSMF validator:** run it in CI in addition to the schema check, only if its licence permits.
   Terms found (2026-10-02):
   - **Validator SDK** (`Relativity.RSMFU.Validator.SDK` 2.4.0 on NuGet, .NET Standard 2.0 / .NET
     Framework 4.6.2) is proprietary. "This software may only be used by persons authorized to use …
     Relativity under a valid license agreement with Relativity ODA LLC." It forbids reverse engineering
     and copying.
     - It is usable in our CI only if the organisation holds a Relativity licence (please confirm).
     - It must stay in a private CI image, never redistributed or shipped.
   - **Sample repo** `relativitydev/rsmf-validator-samples` is BSD-3-Clause (kCura LLC, 2016). Its bundled
     Relativity DLLs are under a separate commercial agreement.
   - **Schema** `RSMFManifestSchema/rsmf_schema_2_0_0.json` is in that BSD-3 repo, so we can vendor it
     with the licence notice.
   - Plan: vendor the schema now. Add the containerized validator only after the licence is confirmed.
5. **Renders:** RSMF creation is limited to `matter_manager` and `tenant_admin` (new permission
   `export.create`) and is audited. Reviewers get previews only.
6. **PDF report:** required. It is rendered from the same HTML template with fixed metadata (creation date,
   producer, document id from the report hash inputs) so output is byte-reproducible. It is hashed and
   recorded as a custody event like the HTML and JSON versions.
7. **UI auth:** backend-for-frontend with server-side sessions, httpOnly SameSite cookies, CSRF protection
   and server-side session revocation. No tokens in the browser.
8. **Anchor storm:** fixed first (migration 0017; docs/runs/2026-10-01-audit-burst.md, "After the fix").
## Open decisions (none blocking M14)
- Confirm the Relativity licence, before adding the RSMF validator to CI (M15).
