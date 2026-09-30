# ADR 0008: Custody package format and offline verifier (`edisc-verify`)

Status: Accepted (2026-09-30), implemented in M5

## Context
Opposing counsel or a court-appointed expert must be able to verify a collection's integrity
independently: no access to our database, no network, no credentials, no trust in our servers.

## Decision
`edisc_custody.export.export_package()` writes one directory per job stream, format
`edisc-custody-package/1`. Every `.jsonl` line is one RFC 8785 canonical JSON object.

| File | Content |
|---|---|
| `manifest.json` | `{format, tenant_id, stream_id, job_id, finalized, head: {seq, hash}, exported_at, objects_included, files: {<name>: {sha256, lines}}}`. The expert quotes the manifest's own SHA-256 in their report |
| `events.jsonl` | every custody event in seq order: `{id, fields, prev_hash, event_hash}`, where `fields` is exactly the hashed object (ADR 0003) |
| `items.jsonl` | every item linked to a batch, **grouped by batch in event seq order, then by `idempotency_key`**: `{id, source, source_item_id, version, item_type, event_kind, idempotency_key, content_hash, raw_hash, evidence_object_id, storage_key, json_path, custody_event_id, unit_key}` |
| `evidence.jsonl` | the job's evidence registry: `{id, storage_key, kind, state, sha256, size_bytes}` |
| `anchors.jsonl` | every version under the stream's anchor prefix, as listed from the bucket: `{key, version_id, body_b64}`, or `{key, version_id, delete_marker: true}` |
| `objects/<sha256>` | optional: exact bytes of each complete page/file object |

### What `edisc-verify <dir>` checks (exit 0 verified, 1 failed, 2 unreadable)
1. Each file's SHA-256 and line count match the manifest.
2. The full chain verification of ADR 0003, reading anchors from the package: links, hashes, batch
   Merkle roots and counts recomputed from `items.jsonl`, anchor agreement, truncation, hidden anchors,
   head, and the seal for finalized jobs.
3. For every item:
   - its `idempotency_key` recomputes from (tenant, source, source_item_id, content_hash) (ADR 0004);
   - its evidence object exists and has the same storage_key;
   - when objects are included: a file item's `raw_hash` equals the object's SHA-256, and a page item's
     `raw_hash` equals the SHA-256 of the RFC 8785 canonical JSON of the fragment at `json_path` in
     that page. `json_path` grammar: `$`, `.name`, `["name"]`, `[index]`.
4. When objects are included: every object re-hashes to its recorded SHA-256 and size.

### Trust model
- The verifier trusts only the anchors. An expert who wants independence from our export re-fetches
  each anchor by `key` + `version_id` directly from the Object Lock bucket (read-only credentials, or
  a copy provided by the bucket owner) and compares.
- An attacker who controls both the DB and the export tooling but not the locked bucket is caught
  (tested: a consistent offline rewrite fails on anchors).
- Dependencies: the Python standard library, `rfc8785` and the pure modules of `edisc_core` /
  `edisc_custody`. A test asserts that the CLI imports no database, S3 or cloud code.

## Consequences
- + Verification is reproducible by third parties with one command, air-gapped.
- + Streaming reads: package size is not bounded by verifier memory (items are grouped by batch).
- − Packages with objects included are as large as the evidence. `objects_included = false` still
  verifies chain, Merkle roots, keys and anchors, but not content.
- − Format changes need a new `format` version. The verifier rejects unknown formats.
