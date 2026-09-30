# ADR 0009: Envelope encryption for connection tokens

Status: Accepted (2026-09-30), implemented in M7 (`edisc_core.kms`, `edisc_core.envelope`,
`edisc_db.connection_tokens`, migration 0007)

## Context
Source credentials (OAuth access and refresh tokens, bot tokens) give read access to a client's entire
workspace. They stay with us (no third-party brokers), must be encrypted per tenant, must never
appear in logs or Temporal history, and must survive key rotation.

## Decision

### Key hierarchy
- **KEK:** one KMS key per tenant (`tenants.kms_key_ref`). AWS KMS in production (`AwsKmsClient`,
  backlog, required before production); `LocalKmsClient` in local/ci only (it refuses to start
  elsewhere).
- **DEK:** a fresh 256-bit data key per sealed secret, from `GenerateDataKey`. Only the KMS-wrapped
  DEK is stored.

### Binding to context
The context `{tenant_id, connection_id, purpose}` (purpose `connection.access_token` or
`connection.refresh_token`) is bound twice:
1. as the **KMS encryption context** of the wrapped DEK;
2. as the **AES-256-GCM AAD** of the data. Tested with a context-blind KMS, where the AAD alone still
   refuses a blob opened under the wrong context.

A blob copied to another connection's row, another tenant's row, or from the refresh column to the
access column fails to decrypt (tested at the DB level via superuser copies).

### KMS interface = AWS KMS semantics
`generate_data_key`, `decrypt` and `re_encrypt` map 1:1 to AWS `GenerateDataKey`, `Decrypt` and
`ReEncrypt`:
- the key id is embedded in the wrapped blob;
- the encryption context must match exactly (no missing, extra or different pairs);
- context values are strings;
- one opaque `InvalidCiphertextError` covers wrong context, tampering, unknown key and disabled key
  versions;
- rotation adds key material while old versions keep decrypting until they are disabled.

The stub enforces all of this (unit-tested), so AWS drops in with no caller changes.

### Blob format (stored in `connections.encrypted_access_token` / `encrypted_refresh_token`)
Canonical JSON: `{v: 1, alg: "AES-256-GCM", key_id, key_version, wrapped_dek, nonce, ct}`. The key id
and version are also recorded in `connections.token_key_id` / `token_key_version`, so rotation can find
stale rows without decrypting.

### Rotation
- `rewrap_tenant_tokens` calls KMS `ReEncrypt` on each wrapped DEK. The data ciphertext and nonce are
  unchanged, and **no plaintext DEK or token enters the process**.
- Each connection row is rewritten atomically (row lock, version bump).
- Tested end to end: rotate, rewrap, disable the old version, and every token still decrypts to the
  same value. Negative control: without the rewrap, disabling the old version makes tokens unreadable.

### Atomic refresh
- Both blobs, the expiry, the key reference and `token_version` are replaced in **one UPDATE**, so a
  reader sees the old pair or the new pair, never a mix (tested with a slow refresher and a concurrent
  reader).
- `refresh_tokens` holds the connection row lock from reading the current refresh token until the new
  pair commits. Concurrent refreshers queue, and with `min_valid_for` they reuse a pair another worker
  just obtained. Tested: 8 concurrent refreshers, 1 provider call.
- If the provider call or the write fails, the transaction rolls back and the row is unchanged.
- Other writers use optimistic concurrency (`expected_version`) and fail with
  `StaleTokenVersionError` instead of overwriting.

### Never logged
- Every sealed or opened token is registered with the redactor (ADR/M3), and tokens exist only as
  `SecretStr`.
- `DecryptionError` messages name the purpose and connection, never material.
- Tested across the full lifecycle, with SQLAlchemy statement **and parameter** logging on, a
  "careless" provider library logging tokens, and decrypt failures and exceptions carrying tokens.
  Opaque (non-pattern) canary tokens make sure value registration, not pattern matching, is what
  redacts them.
- Tokens are never Temporal inputs or outputs: activities load them from the DB by connection id
  (enforced in M12).

### Refresh tokens: designed out where possible (review, 2026-09-30)
- **Microsoft Teams / Graph:** application permissions via the **client-credentials** flow, so there are
  **no refresh tokens**. Our app authenticates with a **certificate**, not a client secret. The
  certificate lives in the secret manager (backlog, required before production). Access tokens are
  short-lived and re-minted on demand.
- **Slack, internal-app tier:** **token rotation stays disabled**, so bot tokens do not expire and
  there is no refresh token to lose.
- **Where refresh tokens are unavoidable** (future providers): the provider's refresh response is
  sealed and **committed to `token_refresh_journal` in its own transaction immediately on receipt**,
  before any other work, and only then applied to the connection row.
  - The connection row is locked `FOR NO KEY UPDATE`, which serializes refreshers without blocking
    the journal's foreign-key check. Plain `FOR UPDATE` deadlocks through the application; found and
    fixed in testing.
  - `reconcile_token_refreshes` runs at worker startup. It applies any `received` entry whose
    `based_on_version` still matches the row, by copying ciphertext: same context, no decryption. It
    marks older entries `superseded`.
  - It finds entries across tenants through `pending_token_refreshes()`, executable only by the
    sweeper login and returning ids only.
  - Tested: a crash between journaling and applying heals on reconcile. A journaled response
    overtaken by a later re-authorization is superseded, not applied.

## Consequences
- + A database dump alone reveals no tokens. Ciphertext moved between rows is useless.
- + Rotation needs no downtime and no plaintext handling.
- − **Residual risk, narrowed:** the only remaining window is between the provider responding and the
  journal commit, a single small insert. If the journal commit itself fails (the database is
  unreachable), the rotated refresh token is lost. The connection then needs re-authorization,
  detected on the next refresh (`invalid_grant`), which marks it `error` with a custody event (M13).
- − Custody events for connection lifecycle (created, validated, refreshed, rotated) are emitted by the
  API/worker layer that owns the tenant custody stream, not by this module.
