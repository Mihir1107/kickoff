# Architecture: Collection Module (Phase 0/1)

Status: living document. Decisions and their rationale live in `docs/adr/`; this file is the map.

## 1. Purpose
Collect employee communications (Slack, Teams, later Google Workspace / M365 mail) for litigation and
investigations so that the collection is **complete, provable and tamper-evident**, and deliver it
review-ready (RSMF per conversation per day, native JSON, HTML preview, collection report).
Hierarchy follows Relativity: tenant (client) > matter > workspace.

## 2. Component map

```
            ┌──────────────┐   start job / status / verify    ┌───────────────────────────┐
  user ───▶ │  apps/api    │ ───────────────────────────────▶ │ Temporal (server + UI)    │
            │  (FastAPI)   │ ◀── reads Postgres (RLS) ──┐     └────────────┬──────────────┘
            └──────────────┘                            │                  │ task queue per source
                                                        │     ┌────────────▼──────────────┐
                                                        │     │ workers/collection        │
                                                        │     │  CollectionJobWorkflow    │
                                                        │     │   └ CollectUnitWorkflow×N │
                                                        │     │  activities ─────────────┐│
                                                        │     └──────────────────────────┼┘
   packages (pure libraries, no service of their own)   │                                │
   ─────────────────────────────────────────────────    │                                ▼
   core        settings, canonical schemas, ids, time,  │
   db          schema (Alembic), models, roles, tenant_tx│   connectors/<source>  (auth, enumerate, fetch raw)
               canonical JSON, logging+redaction        │        │  rate-limit hook ──▶ Redis token bucket
   connectors  base protocol + dummy (+ stubs)          │        ▼
   normalizer  raw → canonical, version fingerprint,    │   normalizer (raw page → canonical items)
               idempotency keys, custodian resolution   │        ▼
   evidence    streaming SHA-256 → S3 multipart, Object │   evidence  ──▶ MinIO/S3 Object Lock (COMPLIANCE)
               Lock retain-until, verify                │        ▼
   custody     append-only hash chain, Merkle batches,  └── ONE Postgres transaction per batch:
               verify_chain, WORM seal                        items + job_items + custody event
   renderers   (Phase 2) RSMF / HTML / JSON                   + checkpoint + counts
```

## 3. Data flow for one batch (the exactly-once boundary, ADR 0006; implemented in `edisc_worker.pipeline`)
1. `collect_pages` activity loads the unit's checkpoint (cursor) from `work_units` under `SET LOCAL app.tenant_id`.
2. It awaits the rate limiter, then connector `fetch` returns one raw page (exact bytes) + next cursor.
3. Evidence (ADR 0002): write-ahead `evidence_objects` row → stream page bytes to WORM (`If-None-Match: *`, lock set at create, rolling retain-until) → mark complete with our streaming SHA-256. Attachments/files are **separate** objects: streamed to the staging bucket while hashing, then copied into the content-addressed WORM key (dedup per tenant) and verified.
4. Normalizer turns the page into canonical items (messages, message versions, reaction snapshot events, files), each with `raw_hash`, `content_hash`, pointer `(storage_key, json_path)` and idempotency key.
5. **One transaction:** `INSERT items … ON CONFLICT (idempotency_key) DO NOTHING`; `INSERT job_items` for every observed item; append one `items_collected` custody event with the batch Merkle root; advance `work_units.cursor`; update `collected_count`. Commit.
6. A crash before step 5 commits replays the page from the old cursor (items dedup, the orphan page object is accounted for by its `evidence_objects` row). A crash after commit resumes from the new cursor.

## 4. Data model (Postgres 16, all tenant-scoped tables under FORCE RLS; ADR 0007)

| Table | Notes |
|---|---|
| `tenants` | id, name, subdomain, kms_key_ref, created_at |
| `matters` | id, tenant_id, name, **retention_until NOT NULL**, created_at |
| `connections` | id, tenant_id, source, external_org_id, plan_tier, granted_scopes, encrypted_token_blob (envelope), status, created_at |
| `custodians`, `custodian_identities` | merge/split emit custody events on the tenant stream |
| `collection_jobs` | + `status` ∈ pending, running, completed, completed_with_gaps, completed_unverified, failed, cancelled; connector_version; chain seal key |
| `collection_scopes` | scope_type ∈ custodian, channel, chat; external_id; date_from/date_to |
| `work_units` | replaces `checkpoints` + `reconciliation`: (job_id, unit_key) PK, conversation_id, day, status, cursor, expected_count NULL, collected_count, recon_status. Views `checkpoints` and `reconciliation` expose the spec'd shapes |
| `evidence_objects` | write-ahead registry of every WORM object: storage_key, kind (page, file, seal, report), sha256, size, state (pending, then complete or missing; final after that, enforced by trigger), retain_until. *Orphan* = complete but referenced by no item (derived, reported) |
| `items` | **append-only** (UPDATE/DELETE/TRUNCATE rejected by trigger). `raw_hash`, `content_hash`, `evidence_object_id` + `storage_key` + `json_path`, `item_type` ∈ message, file, event (+ `event_kind`), `change_hints`, `UNIQUE (tenant_id, idempotency_key)` |
| `job_items` | append-only. (job_id, item_id) PK, unit_key, custody_event_id: what *this* job observed, even when the item already existed |
| `custody_events` | id, tenant_id, stream_id, job_id NULL, seq, event_type, actor, item_id NULL, payload JSONB, prev_hash, event_hash, created_at. Append-only by trigger (UPDATE/DELETE/TRUNCATE rejected) |
| `custody_chain_heads` | stream_id PK, last_seq, last_hash, locked `FOR UPDATE` on append; trigger allows only `last_seq + 1` |

Mutable by design: `collection_jobs`, `work_units` (checkpoints/counters), `connections`, `custodians`,
`custodian_identities`. `matters.retention_until` may only be extended. No table grants DELETE or
TRUNCATE to the app role; see ADR 0007 for roles.

## 5. Temporal topology (ADR 0001)
- Task queue per source: `collect-dummy`, later `collect-slack`, `collect-teams`.
- `CollectionJobWorkflow(job_id)`: `enumerate_units` activity writes `work_units` → pull pending units from DB in pages → run ≤ N `CollectUnitWorkflow` children → continue-as-new every ~500 children → `finalize_job` (reconcile, set status, seal chain to WORM).
- `CollectUnitWorkflow(job_id, unit_key)`: loop `collect_pages(max_pages, max_seconds)` until done; continue-as-new after ~200 iterations.
- Workflow inputs/outputs are IDs and small counters only. No raw data, no tokens.

## 6. Invariants and where they are enforced

| Principle | Enforcement |
|---|---|
| No silent loss | Per-unit reconciliation; job status never `completed` unless every unit has expected == collected (ADR 0005); orphaned evidence and unverifiable units listed in the report |
| Tamper evidence | SHA-256 of every page, file and item; S3 Object Lock COMPLIANCE; `If-None-Match` on writes; verify re-downloads and re-hashes (ADR 0002) |
| Chain of custody | Hash chain per stream; RFC 6962 Merkle root per batch recomputed from items; WORM anchors at lifecycle events, every N batches and a seal at job end; offline `edisc-verify` on exported packages (ADR 0003, 0008) |
| Idempotency | `UNIQUE(idempotency_key)`, key = tenant + source + source_item_id + content_hash (ADR 0004) |
| Resumability | DB checkpoint committed atomically with items (ADR 0006) |
| Tokens stay with us | Envelope encryption per tenant via `KmsClient`; decrypted only inside activities; redaction filter; never in Temporal payloads |
| Tenant isolation | FORCE RLS + non-owner `edisc_app` role + `SET LOCAL app.tenant_id` per transaction (ADR 0007) |
| Streaming | Page-at-a-time processing, multipart upload with rolling hash, bounded queues |

## 7. Environments
`EDISC_ENV` ∈ local, ci, staging, production. Local/ci only: short evidence retention override,
`make nuke`. Everything else behaves identically across environments.
