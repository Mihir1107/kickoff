# Architecture Decision Records

| # | Title |
|---|---|
| 0001 | Temporal as the workflow engine |
| 0002 | WORM evidence storage (S3 Object Lock, COMPLIANCE) |
| 0003 | Chain of custody as a hash chain with Merkle-rooted batches and WORM anchors |
| 0004 | Idempotency key and version fingerprint |
| 0005 | Unit of work = conversation × UTC day; reconciliation semantics |
| 0006 | Exactly-once effects via one transaction per batch |
| 0007 | Tenant isolation with Postgres row-level security |
| 0008 | Custody package format and offline verifier (`edisc-verify`) |
| 0009 | Envelope encryption for connection tokens |
| 0010 | Distributed rate limiting (Redis token buckets) |
| 0011 | Thread replies in range whose parent is out of range (**proposed**) |
| 0012 | Temporal workflow design: fan-out, continue-as-new, errors, cancellation, versioning |
| 0013 | API authentication and authorization: tenant > client > matter > workspace, scoped roles |

New ADRs: copy the structure (Status / Context / Decision / Consequences). Superseded ADRs are kept and marked.
