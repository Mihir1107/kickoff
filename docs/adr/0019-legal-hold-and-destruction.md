# ADR 0019: Storage identities, upload quotas, legal hold, destruction (M18)

Status: **Accepted** (2026-10-20). Built from `docs/plans/m18.md` (the plan) with the user's
decisions of 2026-10-20 (its §12, which wins where it differs from the rest of the plan).
**Built so far:** step 0 (spikes, §1), step 1 (storage identities, §2), step 2 (upload quotas,
§3), steps 3-4 (legal hold in the database and its S3 flags, §4), step 5 (the standalone verifier,
§6). **Not built yet:** steps 6-7 (destruction and its certificate), step 8 (closing docs). Every
place where the build differs from the plan is listed in §5.

## 1. Step 0: what the spikes on the test stack's MinIO showed (2026-10-20)
`pgsty/minio` RELEASE.2026-08-04, `pgsty/mc` RELEASE.2026-09-16; scratch buckets with Object Lock;
IAM users created with `mc admin`. Scripts kept out of the tree (they are one-shot probes); the
results are the record.

| # | Question | Answer |
|---|---|---|
| S0.1 | Does a bucket policy `Deny` (Principal `*` or `{"AWS": ["*"]}`, with or without an `aws:username` condition) apply to an authenticated IAM user? | **No.** MinIO stores the policy (it reads back) but evaluates bucket policies only for anonymous requests: every denied action was allowed for IAM users, with an `s3:*` grant and with a narrow one. |
| S0.1b | Does an explicit `Deny` in the user's OWN IAM policy win over a later `Allow s3:*` policy attached to the same user? | **Yes**, including `NotResource` (writes outside a prefix) and wildcard actions (`s3:PutBucket*`). |
| S0.1c | Can versioning be suspended on a lock-enabled bucket? | No (`InvalidBucketState`), for anyone. |
| S0.2 | `ListObjectVersions` with `Prefix=<key>` | Returns every key with that prefix (`t/k` lists `t/k2` too) and the delete markers: the destruction step must filter `Key == key` exactly. |
| S0.3 | Legal hold on a multipart-completed version | Set by an identity with `s3:PutObjectLegalHold` only; idempotent; `HeadObject` shows it; root's version delete is refused ("WORM protected"); clearing needs the same permission. |
| S0.3b | `GetObjectLegalHold` on a version that never had a hold | `NoSuchObjectLockConfiguration`, not `OFF`: read as "no flag". |
| S0.4 | A `PutObject` carrying `x-amz-object-lock-legal-hold: ON` from an identity without `s3:PutObjectLegalHold` | Refused (AccessDenied): the app cannot set flags as a side effect of a write. |
| S0.5 | Which permission authorizes a delete BY VERSION? | **`s3:DeleteObject`**, not `s3:DeleteObjectVersion` (a user with only `DeleteObjectVersion` is refused; with only `DeleteObject` both a version delete and a delete marker are allowed). |
| S0.6 | Can markers be forbidden while version deletes stay allowed? | Yes: `Deny s3:DeleteObject` with `Condition: {"Null": {"s3:versionid": "true"}}` refuses the marker and allows the version delete. |
| S0.7 | The `edisc_destroyer` definer pattern under the append-only triggers | Not spiked yet: it belongs to destruction (step 6). |

## 2. Storage identities (M18 §3; step 1)
- **Three identities** provisioned by `infra/minio/init.sh` (idempotent; run twice in the tests):
  `edisc-app` (API and every collection/export/render/report worker), `edisc-hold` (the holds
  worker), `edisc-destroy` (the destruction worker). Policies in `infra/minio/policies/*.json`.
  Root (`EDISC_S3_ROOT_USER`) is in no service's settings: compose and `minio-init` only, and the
  tests' tamper fixture (`s3_root`, like the database superuser).
- **The second layer on MinIO is explicit denies in each identity's own policy** (S0.1, S0.1b): the
  app never deletes on evidence (markers or versions), never sets flags, never bypasses governance,
  never changes bucket configuration, writes only under `t/` and `custody-anchors/` (evidence) and
  `t/` and `exports/` (staging); the hold identity never writes, never reads content, never touches
  staging; the destroy identity may delete versions but never markers (S0.6), writes only
  certificates under `t/*/destruction/`. A later over-broad grant cannot undo them (tested per
  identity with an extra `s3:*` policy).
- **Bucket policies are AWS-only** (`infra/aws/*-bucket-policy.json`, `aws:PrincipalArn`
  conditions for roles, marked *confirm on AWS*).
- **Every process refuses to run as root** (`edisc_evidence.identity.assert_least_privilege`): it
  asks for the evidence bucket's policy and starts only on AccessDenied (no service identity is
  granted `GetBucketPolicy`; "no such policy" means authorized, so it is refused too). A wide grant
  is caught by the same probe. Outside local/test/ci, the API and general workers refuse to start
  with the hold or destroy credentials in their settings, the holds worker with the destroy ones.
- **The whole integration suite runs as `edisc-app`**; tampering uses `s3_root`.

## 3. Upload quotas (M18 §4, D10; step 2; migration 0046)
- `tenant_quotas` (set by `python -m edisc_api.admin quota` as the owner; the app role reads only),
  `tenant_quota_changes` (append-only: who, why, before, after), defaults in settings (200 GiB in
  flight, 2 TiB stored, 3 concurrent). `GET /v1/tenant/quotas` (tenant admins): limits and usage.
- Checked in the create transaction under a per-tenant transaction advisory lock; usage is
  COMPUTED from `slack_exports` (§5.2). 409 `quota_exceeded` names the limit and the usage, never
  the declared size; audited `export_quota_refused`; the staged multipart upload is aborted.
- `expire-export-uploads` (hourly): an upload still `uploading` after the TTL becomes `rejected`
  `upload_expired` (a new transition, only with that reason), audited, its multipart aborted.

## 4. Legal hold (M18 §1; steps 3-4; migration 0047)
- **Model:** `legal_holds` (one matter OR one client; identity immutable; release final, with the
  cooling-off end; flag progress never goes back), `legal_hold_release_requests` (one pending per
  hold; the approver is never the requester: a CHECK; expires after 72 h; decisions final),
  `legal_hold_objects` (per hold, the pinned versions whose flag the store confirmed; the clearing
  intent and the clearing), `legal_hold_jobs` (sealed jobs whose objects and links a pass read),
  `tenants.hold_cooling_off_days` (7..90, default 7; `PUT /v1/tenant/settings/hold-cooling-off`).
- **One definition of "held"**, SQL functions used by the triggers, retention extension, the API
  and the worker (`edisc_custody.holds`): `matter_held`, `client_held` (the client or any of its
  matters), `client_directly_held`, `evidence_owner_matters`, `evidence_held`.
- **What a hold blocks:** closing (API 409 `legal_hold`; trigger EA005 on any path, superuser
  included); retention shortening (EA004 already: `retention_until` never decreases; COMPLIANCE
  never shortens); destruction (to be built: it will call `held()` per object and refuse on the
  S3 flag). Retention extension keeps a held matter's evidence locked whether it is open or closed
  and past its retention date, and every locked export of a client held directly.
- **API:** place (matter/client), release request, approval by a second, different tenant admin
  (`second_admin_required` while the tenant has fewer than two active user admins: never an
  operator path), list, get with flag progress; `held` on matters and clients. Every step a custody
  event in the tenant stream, anchored, with an alert. `expire-hold-releases` records expiries.
- **The S3 flags** (`LegalHoldWorkflow`, queue `holds`, `python -m edisc_worker --holds`, the hold
  identity): keyset pages per job (own objects, linked items' objects, renders' and reports'
  objects), then a client hold's exports; one transaction per page with the rows, counts,
  checkpoint and a Merkle-rooted event; a missing version is an incident. `flag-held-objects` (15
  min) re-runs passes so objects linked later get their flag. A release clears only versions no
  other active hold covers; a flag that disagrees with the database is refused and reported (D2).

## 6. The standalone verifier (M18 §5, D12; step 5)
- **`packages/verify`** (distribution `edisc-verify`, module `edisc_verify`) OWNS the offline code,
  moved with `git mv` (history kept): from `edisc_custody` `merkle`, `chain`, `package`, `cli`,
  `archive`, `package_archives`, `package_source`, `rsmf_check`, `render_files`, `report_files`,
  `render_package`, `report_package`; from `edisc_core` `canonical`, `lone_surrogates`,
  `jsonpath`, `idempotency`, plus `utc` (`ensure_utc`, `format_utc` and their errors) and
  `statuses` (`JobStatus`, `UnitStatus`, `ReconStatus`, `ARCHIVE_CAVEAT`), which `edisc_core.time`
  and `edisc_core.schemas` re-export; from `edisc_renderers.report` `model`, `recompute`,
  `version`. Every import in the tree now names `edisc_verify.*`; the old module paths are
  `sys.modules` aliases of the new modules for one release (the same module object, so
  monkeypatching either path patches both). The mutation catalog moved in the same change.
- **Dependencies:** `cryptography==50.0.1` and `rfc8785==0.1.4`, exact pins equal to `uv.lock`, and
  `requirements.lock` with hashes generated from it (a unit test fails on drift). Pydantic is gone
  from the verifier: canonical JSON converts a model by duck typing (`model_dump`), with the same
  bytes as before. Build backend pinned (`hatchling==1.27.0`).
- **The JWS (`edisc_verify.jws`):** one fixed algorithm, **ES256** (D7 allowed ES256 or RS256:
  ES256 has the RFC 7515 Appendix A.3 vector, short signatures, and is available as an AWS KMS key
  spec for step 7). Any other header `alg` (or none, a list, a different case, a duplicated
  member) is refused before any key is used; only `alg`, `kid`, `typ` may appear (so `crit`, `jku`,
  `jwk`, `x5u` are refused); `kid` must be the key's RFC 7638 thumbprint; strict base64url; the
  signature exactly 64 octets `R || S`; detached payloads (RFC 7515 Appendix F) where the
  certificate needs them. Tests: RFC 7515 A.3 verifies, A.2 (RS256), A.4 (ES512) and A.5 (`none`)
  are refused by their header, genuine ES256 signatures over refused headers are refused.
- **Boundary tests** (`tests/unit/verify/test_boundary.py`): every module's imports (AST, functions
  included), a subprocess importing the whole distribution, exact pins, the aliases, no old path
  left in the tree, the version and formats (`edisc-verify --version`), the wheel built twice.
- **CI job `verifier-standalone`** (`make verifier-standalone`): the wheel built twice with
  `SOURCE_DATE_EPOCH` = commit time and compared; `python -m venv`, the locked dependencies
  downloaded once with `--require-hashes`, installed `--no-index`, the wheel `--no-deps`; the venv
  must hold exactly those packages and pip; then `tests/integration/api/test_verifier_standalone.py` makes a job,
  render and report through the API and requires the standalone executable to give the same exit
  status and output as the in-tree verifier on every package and tampered copy.
- Mutation round `m18-verifier`: 8/8 caught.

## 5. Where the build differs from the plan
1. **Bucket policies are not a layer on MinIO** (S0.1). Plan §3.2 put the second layer in bucket
   policies "tested on MinIO"; MinIO does not evaluate them for IAM users. The second layer is
   explicit denies in each identity's policy (tested, including against a later wide grant); the
   bucket policies are written for AWS and marked to confirm there.
2. **Quota usage is computed, not counted** (plan §4: a `tenant_quota_usage` row under `FOR NO KEY
   UPDATE` plus a daily drift check). The app role may not lock a row it cannot update, and a
   computed sum cannot drift, so there is no counter and no drift check: a per-tenant
   `pg_advisory_xact_lock` serializes creates, and the sum over the tenant's exports (by the new
   `(tenant_id, status)` index) is read after it.
3. **The destroy identity has `s3:DeleteObject`** (S0.5): MinIO authorizes version deletes with
   it. Markers are denied by the `s3:versionid` condition (S0.6). The plan's "nobody gets
   `s3:DeleteObject`" holds for the app and hold identities.
4. **The hold identity cannot read content** (stricter than plan §3.1, which listed
   `GetObjectVersion`): it needs only the flag calls.
5. **Clearing records its intent first** (`legal_hold_objects.clear_requested_at`): without it, a
   clearing resumed after a crash between the store's OFF and the record would look exactly like a
   flag removed by someone else (an incident under D2).
6. **A matter hold does not cover its client's exports** (`client_directly_held`): an export
   belongs to no matter (ADR 0014); a matter hold covers the archives its jobs read, through their
   entries.
7. **Sealed jobs are read once per hold** (`legal_hold_jobs`): the 15-minute re-runs would
   otherwise re-read every object of every job.
8. **The hold scope's indexes** (not in the plan): `evidence_objects (job_id, id)`,
   `evidence_objects (archive_evidence_id)` and `items (evidence_object_id)`, so a flag page and an
   "is this object held?" question read bounded rows (CLAUDE.md: bounded by construction). On a
   large production `items` the index is built inside the migration's transaction (not
   `CONCURRENTLY`): a maintenance window, noted for the deployment.
9. **Tenant admin count for the two-person rule** counts active USER principals with
   `tenant_admin` at the tenant directly or through a group's recorded members. An admin who holds
   the role only through an IdP group claim with no recorded membership is not counted: the rule
   then refuses (`second_admin_required`), the safe side.
10. **The verifier's JWS algorithm is ES256** (plan D7 left ES256 or RS256 open; §6).
11. **The verifier states its formats with `--version`** and does not yet carry the destruction
    certificate format (step 7 adds it). The `verifier-v*` release job is not built (BACKLOG: the
    `v*` tag filter would also catch it).
12. **The standalone CI job uses packages made through the API on its own stack** (plan §5 named
    "the corpus job package"): a dummy job with its render and report, plus tampered copies;
    the destruction certificate joins in step 7.
