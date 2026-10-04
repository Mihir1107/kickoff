# edisc-renderers

RSMF 2.0 renderer (`edisc_renderers.rsmf`, ADR 0015). Pure: no database, object storage, workflow or
clock imports (`tests/unit/renderers/test_purity.py`). The worker loader builds `SliceInput`s from
items and derivations and streams file evidence through a `FileOpener`.

- `render_slice(SliceInput, RenderOptions) -> list[RenderedFile]`: one conversation over one local day,
  split into parts of at most `cap` (10,000) events, context included.
- `RenderedFile.stream(opener)`: the `.rsmf` bytes (EML + base64 `rsmf.zip`), streamed. File bytes
  are checked against the recorded size and SHA-256 as they pass.
- `Reconciler`: every in-scope item appears exactly once as an event across the render, plus marked
  context events. Mismatches raise `ReconciliationError`. `Reconciliation.as_payload()` goes into the
  render's custody stream.
- `render_job(...)`: the same for a whole job held in memory (tests, small jobs).
- The vendored manifest schema and its BSD-3 licence are in `rsmf/schema/` (see `SOURCE.md`).
  Every manifest is validated at render time.
- `RENDERER_VERSION` (`rsmf/version.py`) keys the golden bytes in `tests/golden/rsmf/<version>/`.
  Bump it for any change to the output bytes.

HTML preview and native JSON export: later milestones.
