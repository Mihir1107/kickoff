# Architecture Decision Records

| # | Title |
|---|---|
| 0001 | Temporal as the workflow engine |
| 0002 | WORM evidence storage (S3 Object Lock, COMPLIANCE) |
| 0003 | Chain of custody as a hash chain with Merkle-rooted batches |
| 0004 | Idempotency key and version fingerprint |
| 0005 | Unit of work = conversation × UTC day; reconciliation semantics |
| 0006 | Exactly-once effects via one transaction per batch |
| 0007 | Tenant isolation with Postgres row-level security |

New ADRs: copy the structure (Status / Context / Decision / Consequences). Superseded ADRs are kept and marked.
