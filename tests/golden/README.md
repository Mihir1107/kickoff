# Golden datasets

Seeded dummy-connector specs and their expected counts. Each spec is a JSON file:
`{seed, conversations, days, messages_per_unit, ...}` plus the expected totals it must produce.
Changing the dummy generator must not change a golden spec's output. If it does, it is a
connector version bump.

## RSMF renders (`rsmf/<renderer version>/<case>/`)
Byte-exact `.rsmf` files plus `index.json` (SHA-256, sizes, reconciliation summary) from
`tests/unit/renderers/test_golden.py`. They are keyed by `RENDERER_VERSION`, and older generations
stay as history. A byte change needs a version bump, then `EDISC_RECORD_RSMF=1` records the new
directory. Recording never overwrites an existing generation. The files are binary in
`.gitattributes` (CRLF must survive checkout).
