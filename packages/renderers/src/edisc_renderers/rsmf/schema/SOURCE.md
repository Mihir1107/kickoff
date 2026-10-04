# Vendored: RSMF 2.0.0 manifest schema

| | |
|---|---|
| File | `rsmf_schema_2_0_0.json` |
| Upstream | https://github.com/relativitydev/rsmf-validator-samples |
| Upstream path | `RSMFManifestSchema/rsmf_schema_2_0_0.json` |
| Commit | `c717cd322264b46115d27d034a6107c8c91043d8` |
| URL | https://raw.githubusercontent.com/relativitydev/rsmf-validator-samples/c717cd322264b46115d27d034a6107c8c91043d8/RSMFManifestSchema/rsmf_schema_2_0_0.json |
| SHA-256 | `9658446c8ced7c92b1413dac0509a7645cf1578d25e30cb88d3e9c18696e2338` (25,913 bytes) |
| Licence | BSD 3-Clause, Copyright (c) 2016, kCura LLC: `LICENSE` (upstream file at the same commit) |
| LICENSE SHA-256 | `0e8e3a6e5835b99155156cfd13742a315c5f855d75c4a1369e626e931c19290c` |
| Fetched | 2026-10-04 |

The schema file is byte-for-byte the upstream file. Do not edit it: a test checks the SHA-256 above.
To move to another upstream commit, replace both files, update this note and re-run the tests.

**Licence scope.** The upstream `LICENSE` is BSD-3 for the repository's source files. Its last
paragraph puts the listed Relativity/kCura `.dll` files under a separate commercial agreement. Only the
JSON schema is vendored here. No `.dll` and no part of Relativity's validator SDK is used or
redistributed (ADR 0015 §8; `docs/plans/phase-2.md`, decision 4).

**What the schema does not check.** It sets no `additionalProperties: false`. The `maximum: 30` on a
reaction `value` is a numeric keyword, so it has no effect on a string. `idn-email` is checked only
loosely by `jsonschema`. The renderer's own structural checks (`edisc_renderers.rsmf.validate`) cover
the rest: parents, participants, attachment ids and zip entries.
