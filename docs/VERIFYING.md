# Verifying a collection offline

This guide is for an independent expert (opposing counsel's, or court-appointed) who receives a
collection from us and has no access to our systems: no database, no object store, no credentials,
no network. It explains how to get and run the verifier, `edisc-verify`, what each check proves,
which parts of a collection report it recomputes and which it can only check by digest, and how to
read a FAILED result.

You do not need to trust our servers or our staff to use it. You need to trust only:

1. **The WORM anchors.** Every custody stream (a job's, a render's, a report's) writes anchors to an
   S3 bucket under Object Lock in COMPLIANCE mode. Nobody, including us and the cloud account's root
   user, can change or delete a locked version before its retention date. The packages include the
   anchors *as listed from the bucket*. To be independent of our export tooling too, fetch each
   anchor yourself by key and VersionId (see [Checking the anchors yourself](#checking-the-anchors-yourself)).
2. **The verifier's source code**, which you can read. It is a few thousand lines of Python. The
   offline path imports only the Python standard library, `rfc8785` (canonical JSON), `pydantic`
   (through `edisc_core`), and pure modules of this repository (`edisc_core`, `edisc_custody`, and
   `edisc_renderers.report` for the recomputation). A test fails if the verifier imports database,
   S3 or cloud code (`tests/integration/custody/test_package.py`).

## 1. What you receive

| Package | `manifest.json` `format` | What it covers | How we produce it |
|---|---|---|---|
| Job custody package | `edisc-custody-package/3` (`/1` and `/2` still verify) | One collection job: its custody chain, every collected item, the evidence registry, the WORM anchors, and optionally the evidence bytes (`objects/`) | `GET /v1/jobs/{id}/package` (zip) or the directory export |
| Render package | `edisc-render-package/3` (`/1`, `/2` accepted) | One RSMF production of a sealed job: its own custody chain, every output file and native, the job's seal anchor | `GET /v1/renders/{id}/package` |
| Report package | `edisc-report-package/1` | One collection report of a sealed job: its own custody chain, `report.json`, the JSONL lists, `report.html`, `report.pdf`, the job's seal anchor | `GET /v1/reports/{id}/package` |

Every package can be verified as the downloaded zip **or** as a directory. Verify the zip whenever
you can: it is read in place by a hardened zip reader, and nothing is extracted. Two downloads of
the same package are byte-identical (no export time is recorded), so quoting a package's SHA-256 is
meaningful.

A job package may **reference** a Slack export zip by SHA-256 instead of embedding it (exports can
be many GB). You then receive the export zip separately and pass it with `--archive`. A render
package may likewise reference its outputs and natives, which you pass with `--file`.

## 2. Getting the verifier

You need Python 3.12 (we pin 3.12.13; `.python-version`) and [uv](https://docs.astral.sh/uv/).
From the source archive of the repository at the commit we name for the production:

```
uv sync --frozen --no-dev --package edisc-renderers
uv run --no-sync edisc-verify --help
```

`--frozen` installs exactly the versions in `uv.lock` (with their hashes). `edisc-renderers` pulls
in `edisc-custody` (the verifier) and `edisc-core`. It is needed for `--job-package` on a report
package, which recomputes the report from the job's chain. The workspace also installs the database
and S3 client libraries as dependencies of `edisc-custody`, but the verifier never imports them
(tested).

For an air-gapped machine, run `uv sync` on a connected machine with the same OS and Python, then
copy the whole directory, including `.venv`, across. A standalone verifier distribution with a
minimal dependency set is not available yet (docs/BACKLOG.md).

Exit codes: **0** verified, **1** verification failed, **2** the package could not be read or its
format is not supported. `--json` prints the full machine-readable result.

## 3. Running it

```
# a job: the chain, every item, every evidence object
edisc-verify job.zip
edisc-verify job.zip --archive slack-export.zip        # a package that references its export

# a render (RSMF production), and its link to the job
edisc-verify render.zip --job-package job.zip
edisc-verify render.zip --file <output or native>      # a package that references its outputs

# a collection report, recomputed from the job's chain
edisc-verify report.zip --job-package job.zip
```

The verifier detects the package kind from its manifest. Every package is **strict**: a file that
the manifest does not account for makes the package fail. `--tolerate-os-metadata` accepts
`.DS_Store`, `._*`, `__MACOSX/`, `Thumbs.db` and `desktop.ini` in a *directory* (each one is
listed), never in a zip.

## 4. What each check proves

### 4.1 Every package
| Check | Proves |
|---|---|
| Each listed file's SHA-256, line count and size equal the manifest's | The package is what was exported: no file was changed, truncated or swapped afterwards. Quote the manifest's own SHA-256 in your report |
| No file outside the manifest (strict) | Nothing was slipped into the package |
| Every `objects/<sha256>` re-hashes to its name and recorded size | The bytes are the recorded bytes |
| **The custody chain**: every event's `event_hash = SHA-256(prev_hash ‖ RFC 8785 canonical JSON of its fields)`, `prev_hash` links to the previous event, sequence numbers have no gap, every event belongs to this stream and tenant | No event was altered, inserted, removed or reordered after the chain was written, **unless** the whole chain was rewritten from that point. The anchors rule that out (next row) |
| **Anchors**: each WORM anchor (listed from the bucket's object versions) names a seq and hash that equal the chain's; no anchor has a delete marker; no anchor names a seq beyond the head; a finalized stream has a seal anchor naming its exact head | The chain is the one that existed when each anchor was written. A rewritten chain disagrees with the locked anchors. A truncated chain (events deleted) is caught by an anchor beyond the head. A hidden anchor shows as a delete marker. The seal proves the stream was closed at exactly this head |

Anchors are written for every lifecycle event and every N events, so at most the events after the
last anchor are covered by the hash chain alone. A sealed stream has no such tail: the seal names
its final head.

### 4.2 Job custody package
| Check | Proves |
|---|---|
| Every `items_collected` batch: its Merkle root (RFC 6962, leaves = `(idempotency_key, content_hash)` sorted by key) and item count recompute from `items.jsonl` | The set of items each batch committed is exactly the listed set: none added, removed or altered |
| Every item's `idempotency_key` recomputes from (tenant, source, source item id, content hash) | Item identities were not forged. An edit of a message is a new version with its own key, never an overwrite |
| Every item's evidence object exists with the same storage key | Each item points at real stored evidence |
| With objects: a file item's `raw_hash` is its object's SHA-256; a page item's `raw_hash` is the SHA-256 of the canonical JSON of the fragment at its `json_path` in the page | Each message's recorded hash is the hash of the bytes the source returned (lone UTF-16 surrogates are hashed losslessly) |
| Archives (Slack exports): the zip's SHA-256 and size first (an archive that fails is never opened); then each entry by exact name bytes, CRC-32, compressed size, decompressed SHA-256 and size, under the export's recorded limits | Messages collected from an export are bytes of exactly that export file. With `--archive`, the zip is matched by content, not by name |

Without objects (`objects_included: false`), the chain, Merkle roots, keys and anchors are verified,
but not the content. The output says so.

### 4.3 Render package
| Check | Proves |
|---|---|
| The stream starts with `render_started` for this render. It references the job's id, final head, seal key and VersionId, and the included seal anchor names exactly that head | The production was made from that sealed job, in its final state |
| Each `render_files_batch`: Merkle root, count and position of its file records; `render_completed`'s totals and root over the batch roots | The list of output files is complete and unaltered |
| Every output file's and native's SHA-256 and size; each batch's natives root | The `.rsmf` files and the natives (attachments kept beside them) are the produced bytes |
| Each `.rsmf` opened with the hardened reader: the natives it references are exactly those whose records list that file, and every placeholder agrees with its record | No attachment was dropped or substituted between the RSMF and its natives |
| `--job-package`: the job package verifies, and its head and seal are the ones the render references | The render and the job you hold belong together |

### 4.4 Report package
| Check | Proves |
|---|---|
| The report's own chain and seal, as in 4.1; the stream starts with `report_started` for this report, referencing the job's final head and seal anchor | The report was made for that sealed job and has not changed since it was sealed |
| `report_generated`'s file records: their RFC 6962 root, the manifest's outputs, every `outputs/<name>`'s SHA-256 and size | `report.json`, the JSONL lists, `report.html` and `report.pdf` are the generated bytes |
| `report.json` names the same SHA-256 and size for each JSONL list | The summary and the full lists belong together |
| `--job-package`: recomputation, see section 5 | The report states what the job's chain says |

## 5. The collection report: recomputed, checked by digest, taken as written

A report states facts from the **verified job chain** first. The database is a cross-check, and any
disagreement is listed in the report as a divergence, never resolved silently. With
`--job-package`, the verifier folds the job's verified events with the report's own model (the same
code that wrote the report) and compares the results. It never recomputes from a job package that
fails.

### Recomputed from the chain and compared byte for byte
| Content | Notes |
|---|---|
| `units.jsonl`, **byte for byte** | Every unit (conversation-day) row: expected and collected counts, reconciliation status, file gaps, gap reasons, scopes, access-lost reason, error, and status for units left unsettled. Rows are in code-point order (conversation, day, unit key) |
| Unit counts by status and reconciliation status; totals (expected, collected, file gaps) | Derived from those rows |
| The unit exceptions list (capped at 1,000, worst first) and the conversations section, plus `conversations.jsonl` when the report has more than 1,000 conversations | Derived from those rows |
| Job: who started it, its final status, the unit day zone | From `job_started` and the final event |
| Scopes, access facts (connector and version, connection, plan tier, granted scopes, blind spots, export hash) | As `job_started` recorded them |
| Pauses (with who re-authorized) and how many were never resumed | From `job_paused` / `job_resumed` |
| Custody actors by event type | Every event's actor |
| Normalizer versions | From `job_started` and each batch, for jobs started since they were recorded (ADR 0018 §30). Older jobs: checked by digest only, and the output says so |
| **The clean verdict, one way** | See below |

**The verdict.** "Clean" by the chain alone means: one `job_started` and one final event, final
status `completed` (a job collected from an export is never clean, only "complete relative to the
export"), no cancellation or failure recorded, and every conversation-day unit settled, reconciled
and free of gaps, surplus, lost access, unverifiable counts or failures. The report is held to this
rule:

- If the report states clean (`job.clean`, or the clean line in its banner, which the HTML and PDF
  print) and the chain does not give clean, the package **FAILS**. The output names the chain's
  reasons.
- If the report states not clean and the chain gives clean, the package **passes**, and the output
  lists the reasons the report states. A report can know more than the chain: a divergence between
  the chain and the database, a retention gap (evidence that was briefly not under lock), or a custody
  verification failure when the report was made. You cannot recompute those reasons offline, but
  none of them can make a job look better than its chain.

### Checked by digest only (not recomputable offline), and why
| Content | Why it cannot be recomputed from the job package |
|---|---|
| `observations.jsonl` and the observation counts (edits, deletions, reactions, unavailable files, …) | They are the items' derived records. The chain covers each item by its content hash, not by what the normalizer derived from it. You can re-derive them from the evidence bytes with the normalizer of the stated version |
| `renders.jsonl` and the renders section | Each render has its own custody stream. Verify render packages for those |
| The snapshot: retention gaps, evidence lock settings and the S3 observation of them, the job's evidence statistics, the tenant audit events | They are database and bucket state captured once when the report was requested (write-once, digest in `report.json`) |
| The divergences, and each unit row's `divergent` flag | They are a comparison with the database, which you do not have |
| The banner (except the clean line, see above) | It depends on the divergences and retention gaps |
| Unit rows marked `"source": "database"` | Units the chain never mentions: an unsettled unit of a job recorded before ADR 0018 §28, or a database row missing from the chain, which the report also lists as a divergence. The output counts them ("taken as written") |
| Report identity, renderer versions, integrity section | Descriptive. The files' digests in it are checked |
| `report.html` | It is a pure function of `report.json` and the report renderer version (byte-identical per version, no clock, no host data). To re-check it, run that renderer version on `report.json` and compare bytes |
| `report.pdf` | It is rendered from the stored `report.html` inside a pinned linux/amd64 image (fonts, libraries and zlib pinned; the toolchain id is in the report identity). It is byte-reproducible only inside that image |

The verifier prints both lists on every run, so the scope of what was recomputed is part of the
output, not something you have to remember.

## 6. Reading the output

A verified run, for example a report (from the demo, abbreviated):

```
VERIFIED  report package, manifest sha256 0da2c674…
  report stream 01a12f9c…: 2 events, 2 WORM anchors
  head 23dcdaf4…
  6 report files re-hashed
  job package: VERIFIED (2044 items)
  RECOMPUTED from the job chain, identical: units.jsonl byte for byte (41 rows), the unit counts, …
  checked against recorded digests only (not recomputable offline): observations.jsonl …; …
  VERDICT from the job chain alone: NOT clean
    chain: final status completed_with_gaps (job_finished)
    chain: 33 unit(s) gap
```

The first word is the verdict on the package. Then come the counts of what was actually checked.
**Read the counts.** "0 evidence objects re-hashed" means the package carried no objects. That is
still a VERIFIED chain, but the content was not checked.

### A FAILED result
```
FAILED  manifest sha256 ba08715d…
  stream 01a12f9c…: 84 events, 41 batches, 2044 items in Merkle roots, 12 WORM anchors
  2044 items and 0 evidence objects re-hashed
  ERROR archive t/…/files/sha256/1a/1a839bef… (1a839bef…): SHA-256/size do not match the record
        (got 37d82f82…, 225240 bytes); no entry was verified
```

Every `ERROR` line names **where** (a file, a seq number, an anchor key, a row) and **what** did not
hold. Every error is listed, not only the first. How to read them, by kind:

| Message (pattern) | What it means | What it does NOT tell you |
|---|---|---|
| `<file>: does not match manifest (modified after export)` | A listed file was changed after the package was written | Which side changed it: compare with a fresh download (downloads are byte-identical) |
| `<file>: not part of this package` | An extra file (strict packages) | Usually a copy or an OS artefact: re-verify the zip itself |
| `objects/<sha>: bytes do not match the recorded sha256/size` | Evidence bytes differ from what was recorded at collection | Whether the stored evidence (under WORM) differs: fetch that object by key and VersionId |
| `archive <key> (<sha>): SHA-256/size do not match the record …; no entry was verified` | The export zip is not the one locked at upload. None of its entries is opened, so every item collected from it stays unverified | Whether the locked copy differs: fetch it by key and VersionId |
| `archive …: referenced by hash; supply the archive with --archive` | The package references the export instead of embedding it, and you did not pass it | Nothing is wrong yet: pass the export zip you received with `--archive` |
| `seq N: event_hash mismatch (content altered)` | Event N's content is not what was hashed | — |
| `seq N: prev_hash does not link to seq N-1`, `seq gap: expected N, found M` | An event was removed, inserted or reordered | — |
| `<anchor>: seq N disagrees with the WORM anchor (chain rewritten)` | The chain differs from the one the locked anchor recorded | This is the strongest signal of tampering with the records: the anchor cannot be changed |
| `<anchor>: anchors seq N but the chain ends at M (events deleted)` | The chain was truncated | — |
| `anchor <key> has a delete marker …: someone tried to hide it` | An anchor was deleted (Object Lock keeps the version; the marker shows the attempt) | — |
| `stream is finalized but head seq N has no matching WORM seal` | A sealed stream lacks its seal, or the head moved after sealing | — |
| `seq N: batch Merkle root mismatch (item rows altered, added or removed)` | `items.jsonl` does not hold exactly the items batch N committed | — |
| `<item>: idempotency_key does not match its components`, `raw_hash differs …`, `json_path does not resolve …` | An item record is inconsistent with itself or with its evidence | — |
| `render batch … mismatch`, `render_completed counts …`, `native … ` | The render's file or native records are incomplete or altered | — |
| `report_generated: files_root does not match its file records`, `outputs/<name>: bytes do not match …` | A report file was changed after the report was sealed | — |
| `recomputed from the job chain: units.jsonl row K: <unit>: <field>: written X, recomputed Y` | The report states something about a unit that its job's chain does not say. The report's own hashes may all match: this catches a report built to other facts (faulty or compromised report code) | Which statement is true for the source system: the chain records what the collection observed |
| `recomputed from the job chain: <section>: the report states …, the chain gives …` | A recomputed section differs (counts, job, scopes, access, pauses, actors, versions, conversations) | — |
| `recomputed from the job chain: verdict: the report states clean, the chain does not give clean: …` | The report claims a clean collection that its chain contradicts | — |
| `job package: belongs to another job or tenant`, `… its head is not the head the report references` | The two packages you were given do not belong together | — |

Exit code **2** (`cannot read package: …`) means the input is not a package the verifier can parse
(unknown format, a damaged zip, a missing manifest). Nothing was verified. It is not a FAILED, but
it is not a pass either.

A FAILED result localizes a discrepancy; it does not by itself say who caused it. The usual next
steps: re-download the package (it must be byte-identical to the first download), fetch the named
anchors and objects directly from the bucket, and ask us for the custody stream's audit events.

## 7. Checking the anchors yourself

The packages list anchors as our export read them from the bucket. To exclude our export tooling
from your trust base, ask the bucket owner for read-only access (or a copy made under your
supervision) and, for each line of `anchors.jsonl`, fetch the object by `key` and `version_id`.
Compare its SHA-256 and size (`objects/<sha256>` in /2 and /3 packages), and check its Object Lock
mode (COMPLIANCE) and retain-until date. An anchor body is a small JSON document naming the stream,
the seq and the event hash it attests.

## 8. Known limits

- The verifier proves integrity and consistency from collection onward. It cannot prove that the
  source system returned everything. That is what each unit's reconciliation (expected vs collected)
  states, and the report lists every unit that does not match.
- A job collected from an export is complete only relative to that export (the ADR 0014 caveat is
  printed wherever that applies).
- Lone UTF-16 surrogates in source data are hashed losslessly. Everything published shows U+FFFD
  plus the exact code units (`lone_surrogates`), so the verifier can restore and check them.
- The RSMF manifests are validated against Relativity's published JSON schema (vendored, SHA-256
  pinned), not against Relativity's own validator.

Design records: ADR 0003 (custody chain), ADR 0008 (packages and the verifier), ADR 0014 (exports),
ADR 0015 (renders), ADR 0018 (reports, especially §14, §27, §28, §30).
