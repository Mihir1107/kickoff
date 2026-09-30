"""Connection token storage (M7): envelope-encrypted access + refresh tokens, versioned for atomic refresh.

- encrypted_token_blob -> encrypted_access_token; + encrypted_refresh_token.
- token_key_id / token_key_version: KEK recorded with the blobs (rotation finds stale rows without
  decrypting anything).
- token_version: bumped by every write (store/refresh/rewrap) in ONE statement that replaces both blobs
  together; writers use optimistic (expected version) or row-lock concurrency, so a row never holds a
  half-written token set.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE connections RENAME COLUMN encrypted_token_blob TO encrypted_access_token;
ALTER TABLE connections
    ADD COLUMN encrypted_refresh_token bytea,
    ADD COLUMN token_expires_at timestamptz,
    ADD COLUMN token_key_id text,
    ADD COLUMN token_key_version text,
    ADD COLUMN token_version bigint NOT NULL DEFAULT 0,
    ADD COLUMN token_updated_at timestamptz,
    ADD CONSTRAINT ck_connections_token_key_recorded CHECK (
        (encrypted_access_token IS NULL AND encrypted_refresh_token IS NULL AND token_key_id IS NULL)
        OR (encrypted_access_token IS NOT NULL AND token_key_id IS NOT NULL AND token_key_version IS NOT NULL));
"""

DOWNGRADE = """
ALTER TABLE connections
    DROP CONSTRAINT ck_connections_token_key_recorded,
    DROP COLUMN token_updated_at,
    DROP COLUMN token_version,
    DROP COLUMN token_key_version,
    DROP COLUMN token_key_id,
    DROP COLUMN token_expires_at,
    DROP COLUMN encrypted_refresh_token;
ALTER TABLE connections RENAME COLUMN encrypted_access_token TO encrypted_token_blob;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
