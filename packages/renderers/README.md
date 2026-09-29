# edisc-renderers (stub)

**Phase 2. Do not implement yet.**

- RSMF: EML with `X-RSMF-Version` header and one base64 attachment `rsmf.zip`
  (`rsmf_manifest.json` + attachments), validated against Relativity's published schema.
  Custom headers: `X-RSMF-CollectionId`, source hash, connector version.
  Sliced per conversation per 24h, max 10,000 messages per file.
- HTML preview.
- Native JSON export.
- Renderers read canonical items; they never touch raw evidence except to embed it.
