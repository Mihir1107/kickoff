# ADR 0018: Collection report (JSON, HTML, reproducible PDF) and HTML preview (M16)

Status: **Accepted** (2026-10-06), not implemented. Built from `docs/plans/m16.md` (the proposal,
kept as history) with the review decisions D1-D14 of 2026-10-06 folded in, and the PDF spike S1
(§5.9) passed. Two points wait for the mentor (§17); neither blocks the build.
Builds on ADR 0005 (units, statuses), 0014 §4 (archive caveat), 0015 (render custody stream, loader,
determinism, packages, first-byte rule), 0017 (worker images per runtime identity, generalised here).

## Context
- Phase 2 promises a collection report in JSON, HTML and PDF (phase-2 decision 6: the PDF is
  required, rendered from the same HTML with fixed metadata, byte-reproducible, hashed, recorded as
  custody), and an HTML preview of conversation-days for reviewers (decision 5).
- The sealed job chain already records nearly every fact the report needs (`job_started`,
  `unit_reconciled`, `unit_failed`, `job_paused` / `job_resumed` with the re-authorizing actor,
  `job_finished`, `items_collected` batches covering item-level observations). So the report can be
  derived from **verified custody first**, database rows second, and an expert can recompute it
  offline from the job's custody package.
- A sealed job chain is never appended to, so `report_generated` (already a lifecycle event in
  `edisc_custody.chain.LIFECYCLE_EVENTS`) cannot go into the job's stream: a report gets its own
  stream, like a render.
- Not in the chain today: blind spots, plan tier, granted scopes (only in tenant audit payloads and
  `slack_exports.findings`), the time zone of unit days (fixed to UTC by ADR 0005 but not written
  down), evidence lock settings, retention gaps, renders (their own streams).
- Byte identity of HTML is ours to control (pure code). Byte identity of PDF depends on a layout
  engine and its native libraries; it is promised only inside a pinned image, like RSMF bytes inside
  a renderer triple (ADR 0017).

## Decision

### 1. Outputs (per report)
| File | Format | Bounded | Content |
|---|---|---|---|
| `report.json` | RFC 8785 canonical JSON | yes | the summary (§1.1), with SHA-256, size and row count of every `.jsonl` |
| `units.jsonl` | one canonical JSON object per line, LF; ordered by (conversation id, day, unit key) | no | per unit: key, kind, conversation, day label and zone as recorded (§3.3), scopes covering it, status and recon status VERBATIM, expected, collected, file gaps, no-longer-observed, access-lost reason, failure type and text, archive basis and day anomalies |
| `observations.jsonl` | same; ordered by (unit key, item id) | no | every item-level event: `file_unavailable` (file id, reason), `file_became_available`, `access_lost` / `access_restored`, `no_longer_observed` / `observed_again` |
| `renders.jsonl` | same; ordered by render id | no | every render of the job sealed at the snapshot: identity (triple, options hash), status, head, seal, files, natives count and bytes, and every external native (`{file_id}_EXTERNAL.txt`, SHA-256, size; ADR 0015 §11) |
| `conversations.jsonl` | same; ordered by conversation id | no | per-conversation aggregates (only when there are more conversations than the cap) |
| `report.html` | static HTML, one inline stylesheet | yes (§4.4) | rendering of `report.json` |
| `report.pdf` | PDF rendered from the STORED `report.html` | yes (§4.4) | the same, paginated |

#### 1.1 `report.json` sections (each carries its `source`, §7)
1. Job: tenant, client, matter, workspace, job, rerun-of, requester, created / finished / sealed,
   final status VERBATIM, `clean`, `clean_basis`, the archive caveat.
2. Scopes: type, external id, range, thread-parent policy; units per scope.
3. Access: source, connection, access tier, plan tier, granted scopes, blind spots (§7.2); for
   exports the export id, SHA-256, size, validation findings, tier detection, blind spots.
4. Exceptions (before counts): failed, access-lost, gap, surplus and unverifiable units with
   reasons; unavailable files by reason; no-longer-observed and access-lost observations; orphaned
   evidence.
5. Counts: units by status and by recon status (every enum value, zeros included), expected,
   collected, file gaps.
6. Pauses: reason, connection, paused at, resumed at, duration, who re-authorized
   (`job_resumed.actor`); pauses never resumed.
7. Versions: connector, normalizer versions used, report renderer, PDF toolchain id, Unicode, worker
   image digest.
8. Actors: every custody event's actor grouped by action; the tenant audit events about the job
   (render and report requests) up to the snapshot's audit head.
9. Custody verification: `verify_chain` on the job stream with the seal required (events, batches,
   items, files, anchors checked, head, seal key + VersionId listed from S3, errors).
10. Evidence store: bucket Object Lock mode and default retention and versioning as observed at the
    snapshot (with the observation time), retain-until range of the job's evidence, retention gaps
    (ADR 0002) touching it.
11. Renders and natives (summary).
12. Divergences (§8): always present; an empty list means none were found.
13. Integrity: snapshot digest, `.jsonl` hashes, report identity.

The report holds no message text and no file bytes. It holds conversation names, custodian names
and error texts (`unit_failed.error`, up to 2,000 characters, which can quote source data).

#### 1.2 Preview (on demand, not stored)
- One page per (job, conversation, day, page): the day in UTC or a requested time zone (RSMF slicing,
  `local_day_bounds`), at most `PREVIEW_PAGE_EVENTS` = 1,000 events per page.
- Built from the render loader's `SliceInput` (threads, edits, tombstones, reactions, deleted
  messages' reactions only as history (ADR 0015 §18.1), joins/leaves, bots, placeholders for
  unavailable files, marked thread context).
- Attachments are links only, to `/v1/evidence/{id}/content?purpose=preview` (the pinned file
  evidence id). No inline images. URLs inside message text are plain text, never links.
- Each page's header: job status and clean flag, the day's unit status (expected, collected, recon
  status, file gaps, access lost), the archive caveat where archive-relative, and the page identity
  (slice source hash, preview renderer version).
- Sealed jobs only (the loader's rule). Deterministic for the API's runtime (tested); no promise to
  reproduce a preview on a later runtime (it is not a production).

### 2. Access (D9)
| Action | Permission | Roles |
|---|---|---|
| Read a report (files, package) | new `report.read` | tenant_admin, client_admin, matter_manager, auditor, reviewer, collector |
| Request a new report | new `report.create` | tenant_admin, matter_manager |
| Report status, file list, custody verify | `job.read` / `custody.read` | unchanged |
| Preview a conversation-day and its attachments | `evidence.read` (it is evidence content) | tenant_admin, client_admin, matter_manager, reviewer |
RSMF stays `export.create` / `export.read`. **Pending mentor (§17.2):** whether reviewers and client
admins may see conversation and custodian names in the report and preview. Until answered, the build
shows names to every role above; the switch, if it comes, is a redaction mode recorded in the report
identity, never a different code path per caller.

### 3. Byte identity of JSON, JSONL and HTML
#### 3.1 JSON
Canonical JSON (`edisc_core.canonical`), LF, every list in an order the format defines, integers
only (durations in ms, sizes in bytes), times as UTC ISO 8601 with `Z`. No report id, no generation
time, no host name in any output: the report id names storage keys, never bytes. Times shown are
RECORDED times (seal, pauses, the snapshot's S3 observation).
#### 3.2 HTML
- Pure builders `edisc_renderers.report.html` and `edisc_renderers.preview` (no DB, S3 or clock
  imports; the existing purity test covers them). No template engine: a small element builder whose
  only text path is `escape()`.
- Fixed bytes: `<!doctype html>`, UTF-8, one constant inline `<style>` (its SHA-256 in the CSP),
  attributes in a fixed order, LF, our own integer formatting (no locale).
- User strings are never normalised in the data (`report.json` and the JSONL files keep them
  exactly) but are made safe and visible in HTML and PDF: HTML-escaped and wrapped in `<bdi>`;
  **bidi controls** (U+061C, U+200E, U+200F, U+202A-U+202E, U+2066-U+2069) are REPLACED by a visible
  `[U+XXXX]` marker (kept, they reorder the text around them and the marker too: S1 rendered
  `‮evil.exe‬` as `exe.live[E202+U]`); zero-width and other `Cf` characters, `Cc`, `Zl`,
  `Zp` stay and get the marker after them. The marker is ASCII, so a vendored font always covers it.
- Identity: `REPORT_RENDERER_VERSION`, `PREVIEW_RENDERER_VERSION`, Unicode (+ tzdata for the
  preview's time-zone days only).
#### 3.3 Times in the report (D11)
UTC only. Each unit shows its **day label and zone name exactly as recorded in the chain**, with no
tzdata computation: the day label is the date part of `unit_key` (`<conversation>/<YYYY-MM-DD>`,
recorded in `unit_reconciled` / `unit_failed`). The zone name is recorded from now on: `job_started`
gains `unit_day_zone: "UTC"` (§7.2). For jobs started before that, the zone column prints
`UTC (ADR 0005; not recorded in the chain)` and its `source` says so.

### 4. Nothing looks clean that isn't
1. One function decides `clean` for every output: status `completed` AND custody verification ok
   AND no divergence AND no retention gap overlapping the job's evidence (`JobStatus.is_clean` plus
   the last three). `clean_basis` as the API: `source`, `archive`, none.
2. A banner in words (never colour alone) on top of the HTML and in the running header of EVERY PDF
   page: clean → "Complete: every unit reconciled against the source"; `completed_against_archive`
   → "COMPLETE RELATIVE TO THE PROVIDED EXPORT ONLY" + `ARCHIVE_CAVEAT` byte-equal (also next to
   every archive-relative count); `completed_unverified` → "NOT VERIFIED: N units could not be checked
   against a source count"; gaps, failed units, failed, cancelled → "NOT COMPLETE:" + the counts;
   custody failure or divergence → "CUSTODY VERIFICATION FAILED" / "RECORDS DISAGREE" above
   everything, whatever the status. Every page footer: job id, snapshot digest, "page X of Y".
3. Every enum value has a row in the count tables, zeros included, in a fixed order.
4. A fact with no recorded source prints "UNKNOWN (not recorded)", never "none" or an empty list.
5. Statuses appear verbatim next to any wording; `matched_against_archive` is never shown as
   "matched" and never gets the clean mark.
6. Exceptions come before totals. Capped lists (D5): at most 1,000 rows per category in HTML and
   PDF, exact totals always, and the remainder named by file and SHA-256 ("4,812 more in
   units.jsonl, SHA-256 …"). **Order of a capped list: worst status first, then a stable key.**
   Severity order (worst first): `failed`, `access_lost`, `gap`, `surplus`, `unverifiable`,
   `matched_against_archive`, `pending`, `matched`, `not_applicable`; within a status, by
   (conversation id, day, unit key) for units and (unit key, item id) for observations. The JSONL
   files keep their own (key) order; the cap selects by severity, so the first 1,000 shown are the
   1,000 worst.
7. Preview: the day's unit status in the header; a banner for `gap`, `unverifiable`, `access_lost`,
   `failed` days ("Expected 120, collected 117"); reasons for unavailable files; tombstones with the
   earlier text under edits; context marked "context, outside the collection scope"; "0 messages
   (unit status: matched, expected 0)" for an empty day; 404 `not_in_scope` for a day outside every
   scope, never an empty page.

### 5. PDF (D1, D3, D4, D13)
1. **Engine: WeasyPrint** (70.0 in S1), rendering the stored `report.html` plus a constant print
   stylesheet (`@page` size, running header and footer). Pyphen hyphenation is not used.
2. **Pinned image, linux/amd64 only (D3).** PDFs are produced only by report workers on an admitted
   image (§6). Python packages pinned by `uv.lock`; the base image by digest; Debian packages by a
   `snapshot.debian.org` timestamp (S1: `20261001T000000Z`), so a rebuild gets the same packages.
   arm64 workers do not serve the report queue.
3. **PDF toolchain id** (computed at worker start by `python -m edisc_worker.versions`; S1's
   `toolchain` command is the prototype): SHA-256 over the canonical JSON of
   - platform, Python version, Unicode version;
   - every installed Python distribution and version (weasyprint, pydyf, fonttools, tinycss2,
     cssselect2, Pillow, cffi, ...);
   - the runtime versions the native libraries report: Pango, HarfBuzz, FreeType, fontconfig,
     FriBidi;
   - the Debian package versions (with Debian revision) of everything on the layout path: Pango,
     PangoFT2, HarfBuzz and HarfBuzz-subset, FreeType, fontconfig and its config, FriBidi, GLib,
     libthai, libdatrie, graphite2, libpng, brotli, zlib, expat, libffi. (WeasyPrint has no cairo
     since v53; its PDF writer is pydyf, covered as a Python package; font subsetting is
     HarfBuzz-subset, recorded as `harfbuzz_subset_used`.)
   - SHA-256 of every vendored font file, of `fonts.conf`, of the print stylesheet, of the sRGB ICC
     profile (§5.6);
   - the paper size is NOT in the toolchain id; it is in the report identity (§5.7).
   The SHA-256 of every shared library mapped after a render is recorded next to the id in the image
   registry as an informative manifest (a security rebuild changes those hashes; it may keep the id
   only if the goldens still pass, ADR 0017 §1).
   A worker whose id differs from a report's identity refuses it (the ADR 0015 §15 safety net).
4. **Fonts isolated (D1, D13).** `FONTCONFIG_FILE` points at our `fonts.conf`: one `<dir>` (the
   vendored fonts), one fixed `<cachedir>`, no `<include>` (no conf.d, no system dirs, no `~/.fonts`,
   no XDG dirs), cache built at image build, both directories read-only. Vendored: Noto Sans
   (Regular, Bold), Noto Sans Mono, Arabic, Hebrew, Devanagari, Thai (static hinted TTF from
   `notofonts`), Noto Sans SC, JP, KR and Noto Emoji (monochrome) as **TrueType variable fonts from
   `google/fonts` instanced to a static Regular (wght 400) at image build** with timestamps not
   recalculated. All pinned by commit URL and SHA-256 (`fonts.lock`). The CID-keyed CFF CJK files
   (`noto-cjk` SubsetOTF) are NOT used: subset by HarfBuzz they failed veraPDF (glyphs missing from
   the embedded program, `.notdef` references, width mismatches: rules 6.2.11.4.1, 6.2.11.5,
   6.2.11.8) although poppler drew them, i.e. a stricter viewer may not. Fonts are subset on embed (WeasyPrint's
   HarfBuzz subsetter; never `full_fonts`). The OFL licences (and the Noto CJK licence) are
   committed under `packages/renderers/src/edisc_renderers/report/fonts/LICENSES/` with a
   `SOURCE.md`.
5. **No missing glyphs.** Before HTML for the PDF is built, a pure pass replaces every character the
   vendored fonts' cmaps do not cover with `[U+XXXX]` (the coverage set is computed from the fonts at
   build and is part of the toolchain id through their hashes); `Cf`/control characters keep a marker
   next to them. `report.json` keeps the exact string.
6. **Fixed metadata (D4).** `/CreationDate` and `/ModDate` = the job's `sealed_at` (recorded). The
   generation time lives only in the report's custody stream (`report_started.created_at`, and the
   snapshot's S3 observation time), never in the PDF. `/ID` first half = the first 16 bytes of
   SHA-256(`report.html`), second half pydyf's digest of the content (deterministic, S1). `/Producer`
   `WeasyPrint <version>` (pinned). Uncompressed streams (`uncompressed_pdf=True`): deflate bytes
   depend on the zlib build (the ADR 0015 §6 reason for STORED zips).
   **PDF/A-2u** with the sRGB IEC61966-2.1 (sRGB2014) ICC profile as output intent. The profile is
   vendored in the repo (`report/icc/sRGB2014.icc`, SHA-256
   `384b832de3412066743b52a75ee906b6fb9fb8d9e09e936fc2c43223815c6e0a`, 3,024 bytes, ICC v2, embedded
   profile id `3d0eb2deae9397be9b6726ce8c0a43ce` equal to the MD5 recomputed over the profile, i.e.
   self-consistent; this is the copy WeasyPrint 70.0 bundles). When vendoring (step 3), compare it
   byte for byte with the file from color.org's sRGB profiles page, downloaded by hand in a browser
   (its bot protection served HTML to scripted downloads in S1), and record both in `SOURCE.md`.
   WeasyPrint embeds its own bundled copy, so the worker refuses to start unless that copy is
   byte-equal to the vendored one. veraPDF (`verapdf/cli` 1.30.2, pinned by digest) validates the
   output in CI (§15).
7. **Paper size is part of the report's identity (D4)**: (job, snapshot digest, report renderer,
   toolchain id, Unicode, paper). Allowed `letter`, `a4`. **Default pending mentor (§17.1)**; until
   answered the build uses `letter` and the default is one constant, so a change of default creates
   new identities and never changes an existing report's bytes.
8. **Goldens (D3):** PDF goldens are authoritative only in CI on linux/amd64, inside the admitted
   image. Locally they run under amd64 emulation in that image (`make test-report-pdf`, Docker) or
   are skipped with a visible reason (`pytest.skip("PDF goldens need the pinned linux/amd64 report
   image: run make test-report-pdf")`); a test that would compare against a different toolchain id
   fails, it never passes silently. JSON and HTML goldens run everywhere.
9. **Spike S1 (2026-10-06): passed.** See §5.10.

### 5.10 S1 results (2026-10-06; harness `spikes/m16-pdf/`, workflow `spike-m16-pdf`)
**Input:** a report-shaped document at the D5 cap: six capped sections of 1,000 rows each, a
per-status table and a table of hard strings: CJK (SC, JP, KR), Arabic, Hebrew, mixed bidi, emoji
(skin tone, flag, a ZWJ family), combining marks (stacked accents, Zalgo), Devanagari conjuncts,
Thai, zero-width characters (U+200B, U+200C, U+FEFF), a bidi override (U+202E), and U+10000 /
U+13000 (covered by no vendored font). Letter, 234 pages; one A4 render as well.

**Byte identity: PASS.** 20 runs per variant, each a separate process, with varied `TZ`, `LANG` /
`LC_ALL`, `PYTHONHASHSEED` and `HOME`, four at a time:
| Host | CPU | Image | plain | PDF/A-2u | deflate (info) |
|---|---|---|---|---|---|
| 1: GitHub runner (run 37366348776, host-a) | AMD EPYC 9V45, native x86_64 | config `09470913…`, built on host 1 | 20/20 one hash | 20/20 one hash | 20/20 one hash |
| 2: Mac (Apple silicon), Docker Desktop | x86_64 under Rosetta ("VirtualApple") | the SAME image as host 1, saved there and loaded (config `09470913…`, 10 identical layers) | 20/20, equal to 1 | 20/20, equal to 1 | 20/20, equal to 1 |
| 3: GitHub runner (run 37369595037, host-a) | AMD EPYC 9V74, native | `f288b662…`, built independently on host 3 | 20/20, equal to 1 | 20/20, equal to 1 | 20/20, equal to 1 |
| 4: GitHub runner (run 37369595037, host-b) | AMD EPYC 7763, native | the SAME image as host 3, loaded | 20/20, equal to 1 | 20/20, equal to 1 | 20/20, equal to 1 |
The criterion "two hosts, same amd64 image" is met twice (1 + 2, 3 + 4): every one of the 60 files
on each host has the same SHA-256 as on its partner, and all four hosts agree. Hashes: plain
`00c66fb31e54a1d0249a6da2944246c168d427c8adde43e55ed42373058fef5e`, PDF/A-2u
`b5317047b5a0a286746623a488c0b676524c679a597a5c7e27b548b5fb54743f`, deflate
`95180b1fb05cd3e4db499a1373ed0687ee9879199484c6ebb5c6db92aa63b74e`; A4 plain
`0fc44be9cb230a6f72916cad80fca0eb6a81b8cee4b9323942cd426227e6a84d` (equal on all hosts). The same
hashes also came out of an image built independently on the Mac (another image id, same pinned
inputs), so the snapshot-pinned build reproduces the bytes too.

**Font isolation: PASS** on every host. `fc-list` under our config lists exactly the 11 vendored
files. The probe font (Noto Sans Linear B, covering U+10000) installed in `/usr/share/fonts`,
`/usr/local/share/fonts`, `~/.fonts`, `~/.local/share/fonts` and an `XDG_DATA_HOME` dir, plus the
host's fonts mounted at `/usr/share/fonts/host` (53 on the runner, 80 macOS system fonts on the Mac),
with a raw document still containing U+10000 and U+13000: the PDF is byte-identical to the one
rendered before, and embeds no probe font. Control: a config that also scans those dirs DOES embed
`Noto-Sans-Linear-B` (and on the Mac, macOS's `.LastResort` for U+13000), so the check can fail.

**PDF/A-2u: PASS** (veraPDF 1.30.2) after one change. Round 1 used the `noto-cjk` SubsetOTF (CID-keyed
CFF) fonts and FAILED rules 6.2.11.4.1, 6.2.11.5 and 6.2.11.8 (glyphs missing from the HarfBuzz-subset
CFF programs, `.notdef` references, width mismatches) although poppler drew the glyphs. Round 2 uses
TrueType CJK fonts (§5.4) and passes. Round 1 also showed the bidi-override marker reversed by the
override; bidi controls are now replaced (§3.2), checked on the rendered page.

**Structure:** 234 pages; `/Producer` `WeasyPrint 70.0`; `/CreationDate` = `/ModDate` = the recorded
seal time; `/ID` first half = first 16 bytes of SHA-256(HTML); no `/JavaScript`, `/OpenAction`,
`/AA`, `/Launch`, `/URI`, `/EmbeddedFile`; banner text on the first and last page, "page 234 of 234";
the `[U+10000]` marker present; 10 embedded fonts, all subset, all vendored (`Noto-Sans-JP` unused:
the SC font, earlier in the fallback order, covers kana and kanji).

**Sizes at the cap (Letter, 234 pages):** `report.html` 1,507,124 bytes; PDF uncompressed
18,707,132 bytes (plain) and 18,711,434 (PDF/A-2u); the same with deflate 1,735,554 (10.8x
smaller); A4 18,694,611. Render time about 30 s for one render on a runner, 47 s (EPYC 9V45/9V74)
to 84 s (EPYC 7763) with four concurrent, 50-60 s under Rosetta; peak RSS about 1.16-1.18 GB per
render.
- Consequence: uncompressed (D4 recommendation) costs about 17 MB per capped report. Deflate output
  was byte-identical everywhere in S1 too, because zlib is pinned by the Debian snapshot; the
  remaining risk is a zlib change in a security rebuild, which the admission goldens would catch
  (the image would not be admitted). Kept: uncompressed, as decided; revisit only with the mentor.

**Toolchain id** (S1 image): `c6c9d1d65532920f381975235716f9f6d07a5827202158150850bea9c93f650f`,
equal on every host, over:
- platform `linux/x86_64`, Python 3.12.13, Unicode 15.0.0;
- runtime libraries: Pango 1.50.12, HarfBuzz 6.0.0, FreeType 2.12.1, fontconfig 2.14.1, FriBidi
  1.0.8;
- Debian packages: libpango-1.0-0 / libpangoft2-1.0-0 1.50.12+ds-1, libharfbuzz0b /
  libharfbuzz-subset0 6.0.0+dfsg-3, libfreetype6 2.12.1+dfsg-5+deb12u4, libfontconfig1 /
  fontconfig-config 2.14.1-4, libfribidi0 1.0.8-2.1, libglib2.0-0 2.74.6-2+deb12u9, libthai0
  0.1.29-1, libdatrie1 0.2.13-2+b1, libgraphite2-3 1.3.14-1+deb12u1, libpng16-16 1.6.39-2+deb12u5,
  libbrotli1 1.0.9-2+b6, zlib1g 1:1.2.13.dfsg-1, libexpat1 2.5.0-1+deb12u3, libffi8 3.4.4-1;
- Python distributions: weasyprint 70.0, pydyf 0.12.1, fonttools 4.66.1, tinycss2 1.5.1, cssselect2
  0.10.1, tinyhtml5 2.1.0, pillow 12.3.0, cffi 2.1.1, pycparser 3.0, pyphen 0.18.1, brotli 1.2.0,
  zopfli 0.4.3, webencodings 0.6.1, pypdf 6.19.0 (test only), pip 25.0.1 (to drop from the id in the
  real build);
- `harfbuzz_subset_used: true`; the 11 font files' SHA-256 (in `toolchain.json`); `fonts.conf`
  `3cdb56b7…f98`; sRGB2014 ICC `384b832d…c0a`.
- Informative: 77 mapped shared libraries with their SHA-256 (not part of the id).

### 6. Worker images and queues (D2: ADR 0017 joined and generalised)
- ADR 0017's "renderer triple" becomes a **runtime identity per output kind**. One image may serve
  several identities; `deploy/render-images.json` records, per image digest, the identities it
  serves (`rsmf: r1.3.1-u15.0.0-tz2026e`, `report: r1.0.0-p<toolchain12>-u15.0.0`), git commit,
  build date, status, retention.
- Report queue: `reports.r<renderer>.p<toolchain12>.u<unicode>`; workers poll the queues of their
  own runtime; the routing check and `unroutable` episodes apply as for renders (render episodes are
  generalised to `production_episodes` keyed by (kind, id), migration in step 6).
- Admission (ADR 0017 §1): inside the image, the report oracle cases pass and the report golden
  generation matches byte for byte; the image digest goes in `report_started`.
- Image retention counts report productions too (ADR 0017 §2). Report reproductions
  (ADR 0017 §4 for reports) are backlog.

### 7. Sources
#### 7.1 Where each fact comes from
| Fact | Primary source | Cross-check |
|---|---|---|
| job, scopes, connector version, requester | `job_started` (verified chain) | `collection_jobs`, `collection_scopes` |
| per-unit expected / collected / recon / gaps | `unit_reconciled`, `unit_failed` | `work_units` (additive digest, §12) |
| item-level observations | `items` via `job_items`, covered by `items_collected` Merkle roots | counts vs `unit_reconciled.file_gaps` / `no_longer_observed` |
| pauses, who re-authorized | `job_paused` / `job_resumed` | `job_pauses` |
| final status, unit summary | `job_finished` / `job_cancelled` | `collection_jobs.status` |
| plan tier, granted scopes, blind spots, unit day zone | `job_started` (new jobs, §7.2) | `connections` row |
| normalizer versions | `item_derivations` of the linked items | none |
| renders, natives | each render's sealed stream, `render_files`, `render_natives` | render seal listed from S3 |
| retention gaps, lock settings | the snapshot | none |
Each section of `report.json` carries `source` (`job_chain`, `database`, `s3_observation@<time>`,
`not_recorded`), so a reader knows what an offline verifier can re-derive.
#### 7.2 Recording what the chain lacks (D7)
`job_started` gains, for jobs started after this change: `plan_tier`, `granted_scopes`,
`blind_spots` (from the connection, and for exports the export's `findings.blind_spots` plus the
export id and SHA-256), `unit_day_zone: "UTC"`. A custody payload addition: older events stay valid;
the verifier treats the fields as optional. **Older jobs print UNKNOWN** for blind spots, plan tier
and granted scopes; there is no lookup in the tenant audit stream (not built, by decision).

### 8. Divergences (D8)
Any disagreement between a primary source and its cross-check (a `work_units` row unlike its
`unit_reconciled`; two reconciliation events for one unit; a status unlike `job_finished`;
observation counts unlike the unit's) is listed in `report.json` (`divergences`), makes the report
not clean, records `audit.report_divergence` and raises one `report_divergence` alert. The chain
value is the one stated as the fact. A chain that fails verification still gets a report that leads
with the failure (plus an alert); `report_refused` is only for "job not sealed".

### 9. Lifecycle, custody, audit (D6)
- Records: `reports` (status `requested → snapshotted → generating → generated → completed`, or
  `refused` / `failed`; guard trigger on transitions and on identity), `report_files` (insert-only),
  `evidence_objects.report_id`. One live report per identity (partial unique index).
- **Snapshot** (tx, `requested → snapshotted`): every mutable input captured once into
  `reports.snapshot` (write-once) and its digest: renders sealed now (id + seal head), retention
  gaps touching the job, the S3 lock settings observed now (with the time), the tenant audit head
  (seq, hash) bounding the audit events used.
- **`report_started`** (tx, `snapshotted → generating`) only after: job sealed; its chain verifies
  with the seal required; the seal key is exactly one S3 object version anchoring the head (listed
  from S3). Payload: job ref (id, status, basis, final head, seal key + VersionId), snapshot digest,
  identity (renderer, toolchain id, Unicode, paper), image digest, requester, and for manual
  requests the **reason**.
- Files (§11), each recorded in `report_files` when complete; then **`report_generated`**
  (lifecycle; tx, `generating → generated`): every file (name, media type, SHA-256, size, VersionId,
  rows), the RFC 6962 root over the file records, the clean verdict, divergence count.
- **Seal** (forced anchor) + `audit.report_completed` in one tx. `report_failed` is sealed too;
  `fail_report` is retried without limit; stuck sealing opens an episode (ADR 0015 §15/§16 pattern).
- **Automatic:** the maintenance schedule `ensure-job-reports` starts a report (actor `system`, paper
  = default) for every sealed job without one, failed and cancelled jobs included. No command change
  in `CollectionJobWorkflow`.
- **Missing report episode (D6):** a sealed job with no completed report after
  `EDISC_REPORT_MISSING_SECONDS` (default 3,600) opens a `report_missing` episode with ONE alert; the
  episode closes when a report completes (`end_reason` `report_completed`); losing it again (cannot
  happen once completed) would open a new one. Same table, uniqueness and history rules as the
  `unroutable` render episodes.
- **Manual regeneration:** `POST /v1/jobs/{id}/reports` (`report.create`, recent sign-in hook) with
  a required `reason` (1-2,000 characters) and optional `paper`. It always takes a new snapshot; an
  identical identity returns the existing report (200), otherwise a new report (201). Who (actor) and
  why (reason) go in `audit.report_requested` and `report_started`. **Earlier reports are never
  replaced or hidden**: `GET /v1/jobs/{id}/reports` lists all of them with snapshot time, identity,
  status and requester.
- Tenant audit: `audit.report_requested`, `audit.report_completed` / `_failed` / `_refused`,
  `audit.report_divergence`, `audit.report_read`, `audit.report_package_read`,
  `audit.report_read_aborted`, `audit.preview_read`, `audit.preview_refused`.

### 10. Reads anchored before the first byte
- **Report inputs:** chain verified (seal required, seal listed from S3) before `report_started`;
  renders re-checked against their seals at the snapshot; the PDF is rendered from `report.html` READ
  BACK from WORM by pinned VersionId and checked against its recorded SHA-256.
- **Report bytes served:** `GET /v1/reports/{id}/files/{name}/content` and
  `GET /v1/reports/{id}/package` (`edisc-report-package/1`) follow the render rules: audit committed
  AND `audit.anchor_now` before the response starts; streamed re-hash, a mismatch aborts with
  `audit.report_read_aborted` + alert; exact `Content-Length`; anchor divergence recorded
  (ADR 0015 §19.14). Each route gets a `first_byte.py` test. Only completed (sealed) reports are
  served; `/v1/evidence/{id}/content` never serves report rows.
- **Preview:** authorize → load the slice (pinned versions, checked as the loader does) → reconcile
  the slice (every in-scope message of the day once) → render (bounded by the page cap) → SHA-256 →
  commit `audit.preview_read` (job, conversation, day, zone, page, slice source hash, preview
  identity, output SHA-256 and size) → `audit.anchor_now` → first byte. A failure before that records
  `audit.preview_refused` (integrity → alert).
- **Preview headers (D10):** `Content-Security-Policy: default-src 'none'; style-src 'sha256-<style>';
  img-src 'none'; font-src 'none'; connect-src 'none'; media-src 'none'; object-src 'none';
  frame-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox
  allow-top-navigation-by-user-activation` (also as `<meta http-equiv>` in the document),
  `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`.
  Tested: remote URLs in message content cause NO fetch (§15).
- **Cost (D14):** one forced WORM anchor per preview page view, measured in step 7
  (`scripts/measure_audit_burst.py`). If too costly, the fallback is **group commit**: concurrent
  page views of a tenant wait (bounded, e.g. 50 ms) for one forced anchor that covers all of their
  committed audit events, each response still starting only after its own event is covered. The
  anchor is never skipped.
- M17 risk (not decided here): a sandboxed document has an opaque origin, so a SameSite=Strict
  session cookie may not accompany an attachment link click; the M17 Playwright flow must cover it.

### 11. Storage and retention
`report` registry rows (origin `report`) at `t/{tenant}/reports/{report}/{name}`, written like
productions (hashed while streaming, multipart, Object Lock at create, our SHA-256, per-key content
lock). `evidence_objects.report_id`; retention and the extension job resolve report → job → matter;
report anchors carry the report id.

### 12. Scale (millions of conversation-days)
- One streaming pass per file, memory O(page): keyset pages in file order, short transactions, no
  transaction across S3 I/O, linked items selected BY ID then filtered in Python.
- Chain cross-check in O(1) memory: the chain pass folds each `unit_reconciled` / `unit_failed`
  record into an additive digest (the `_add_digests` construction) and per-status counts; the
  `units.jsonl` pass folds the same canonical records. Both also fold into 4,096 buckets by
  SHA-256(unit key) (128 KiB); only mismatching buckets are re-read and compared unit by unit.
- Index `work_units (job_id, conversation_id, day, unit_key)` for the file order.
- Capped HTML/PDF (§4.6); the full lists live only in the JSONL files. S1 measured the PDF at the
  cap (§5.10).
- Preview: a day-bounded loader entry `RenderLoader.day_slice(conversation, window, page)` (the
  current `_index` loads a whole conversation).
- `scripts/measure_report.py`: 10k, 100k, 1M units; peak memory flat 10k vs 100k (asserted); the 50k
  acceptance job gets a report checked against its oracle.

### 13. Crash points
Status fences moved in the same transaction as the event; each point at the 1st and 2nd occurrence
where it repeats: the snapshot tx; after verification and inside / after `report_started`; mid-upload
of each `.jsonl`, after the object before its row, after the row; after `report.json`, after
`report.html`, mid-PDF, after the PDF object before its row; inside / after `report_generated`; the
seal sub-steps with REAL SIGKILLs (`EDISC_TEST_REPORT_BARRIER`, test/ci only); the failure and
refused paths; `ensure-job-reports` racing a manual request; two identical requests racing; the
anchor sweeper racing a recovering report. Expected: the oracle's bytes, every event and audit once,
one object version per key, no pending row, a verified chain and package.

### 14. Offline verification (D12)
`edisc-verify` learns `edisc-report-package/1` (report chain, referenced job seal, every file,
anchors listed from S3, strict: nothing unlisted). With `--job-package` it **recomputes**
`units.jsonl`, `observations.jsonl` and the chain-derived sections of `report.json` from the job
custody package with the same pure code (`edisc_renderers.report.model`, DB-free and importable by
the verifier) and compares bytes. Snapshot, audit and S3-observation facts are checked against their
recorded digests only, and the output says so.

### 15. Tests (expected values from oracles, never from collected data)
- **Report oracle** (`tests/integration/report/oracle.py`) from `Dataset` + the scenario's injected
  conditions. Named cases: clean; gaps; unverifiable; surplus; failed units; cancelled; failed job;
  pause + re-authorization by a named principal; access lost and restored; no-longer-observed across a
  rerun; each file-unavailable reason; export (`completed_against_archive`, caveat byte-equal,
  findings, blind spots); overlapping multi-scope; renders with external natives; a retention gap;
  a pre-change job (UNKNOWN blind spots, zone note). A coverage matrix must be exercised.
- **Never-clean property** (hypothesis over report models plus the real cases): clean label iff
  the clean function; otherwise the banner words on EVERY PDF page (text extraction) and in the
  HTML; caveat byte-equal; every enum row present; capped lists ordered worst first then by key.
- **Determinism:** two generations (different report ids, processes, `PYTHONHASHSEED`, `TZ`,
  `LANG`, frozen clocks at different instants, poisoned system fonts and zoneinfo) → identical bytes
  for every file; goldens per identity (`EDISC_RECORD_REPORT=1`, never overwrites, refused in CI).
- **PDF structure** (pypdf): `/ID` and dates as specified; no `/JavaScript`, `/OpenAction`, `/AA`,
  `/Launch`, `/URI`, `/EmbeddedFile`; only vendored fonts, all embedded and subset; no `.notdef`
  glyph used; the hard strings extract as the glyph or the marker; veraPDF PDF/A-2u passes; the
  font-isolation leak test of S1 (probe font installed in every default location and host fonts
  mounted → identical bytes, no probe font; a control config that scans those dirs does embed it).
- **Sanitiser** (both renderers; hypothesis over every user-string field): html5lib parse shows only
  allowed elements and attributes, no handlers, no URL except
  `/v1/evidence/{uuid}/content?purpose=preview`, one `<style>` whose hash is the CSP's, every
  invisible character revealed.
- **No remote fetch from a preview (D10):** a corpus day whose messages contain `https://` URLs,
  `<img src=…>` / `<link>` / CSS `url(…)` / `@import` text and `//host` forms is rendered; (a) the
  HTML holds none of them as attributes or CSS (html5lib), and (b) a headless Chromium (Playwright,
  pinned) loads the page with the CSP header from the real API while a local HTTP server listening
  on the referenced host records requests: zero requests, and the browser's CSP report shows nothing
  attempted outside `'self'`.
- **Preview oracle:** the RSMF corpus oracle per conversation-day (every in-scope message once,
  edits, tombstones, reaction history rule, placeholders, context, DST days, +05:45), pages at N and
  N+1 events, one day of a years-long conversation with a bounded number of queries.
- **API:** permission matrix per role (403/404) for every route; first-byte tests (report file,
  report package, preview); audit payloads (preview output SHA-256 equals the bytes served); the
  credential scan; headers; attachment links resolve to the pinned evidence ids; 404 `not_in_scope`;
  409 on unsealed jobs; manual regeneration requires a reason and never hides earlier reports.
- **Episodes:** `report_missing` opens once after the threshold, one alert, closes on completion.
- **Custody, crash, verifier:** §13 matrix; package verification with recomputation; tamper tests
  (a `work_units` row → divergence shown, alert, not clean; a stored report version → read aborts
  with alert; a dropped `units.jsonl` line → verifier fails).
- **Mutation checks:** one catalog entry per protection (clean function, banners, caveat, zero rows,
  UNKNOWN, severity order, escaping, `<bdi>`, reveal, CSP, glyph coverage, `/ID`, uncompressed
  streams, font isolation, ICC check, toolchain refusal, digest cross-check, divergence, audit and
  anchor before the first byte (three routes), stream re-hash, PDF-from-stored-HTML, status fences,
  verifier recomputation and strictness, the missing-report episode).

### 16. Build order (each step: implement → tests → mutation entries → run → commit → push)
1. Report model, pure (`edisc_renderers.report.model`): records in, `report.json` + JSONL out; the
   `job_started` additions (§7.2); worker loader; oracle tests.
2. HTML builder, sanitiser, banners, severity-ordered caps; goldens.
3. PDF: the report image (from `spikes/m16-pdf`: fonts, `fonts.conf`, ICC, snapshot apt), the
   toolchain id in `edisc_worker.versions`, CI job on amd64 in the image, veraPDF, goldens.
4. Migration 0030 (`reports`, `report_files`, `evidence_objects.report_id`, `production_episodes`,
   the `work_units` index, permissions), `ReportWorkflow`, custody stream, `ensure-job-reports`,
   `report_missing` episodes, crash matrix.
5. API routes with first-byte tests, report package, `edisc-verify` (with recomputation).
6. Preview: day-bounded loader entry, pure renderer, route, CSP and no-fetch test, audit-burst
   measurement (D14).
7. Docs: CLAUDE.md section, HANDOFF, ADR 0017 amendment (runtime identities), BACKLOG (report
   reproductions).

### 17. Pending mentor questions (do not block the build)
1. **Default paper size** (Letter for the US market is likely, A4 elsewhere). Paper is in the
   identity; the default is one constant.
2. **Name visibility:** may reviewers and client admins see conversation and custodian names in the
   report and the preview? If not: a redaction mode in the report identity.

## Consequences
- Plus: every statement in the report traces to the verified chain or a recorded snapshot, and an
  expert can recompute the chain-derived part offline.
- Plus: a non-clean job cannot produce a page that looks clean, even an excerpted PDF page.
- Plus: PDF bytes are reproducible for as long as the image is kept (ADR 0017 retention).
- Minus: an amd64 image with about 20 MB of fonts per report runtime; PDF tests need Docker locally.
- Minus: uncompressed PDFs are larger (§5.10); bounded by the cap.
- Minus: one forced anchor per preview page view until measured otherwise.
