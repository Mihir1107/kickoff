"""The token journal holds a token only until it is applied (the fixes to M17 step 6).

- **Entries are deleted once resolved.** An entry exists to recover tokens a provider issued
  before our connection row had them (a refresh response, an install's exchange). Once applied (or
  superseded by a later write) it is a second copy of a live or dead token for nothing: the apply
  (and the reconciler) delete it in the same transaction. The app role may DELETE an entry, and the
  guard trigger refuses deleting one still ``received`` (that would lose a token we owe a
  connection). TRUNCATE stays refused. Resolved entries left by earlier releases are deleted here.
- **Never plaintext.** Both token columns must hold a sealed ``SecretBox`` envelope (canonical JSON
  ``{v: 1, alg, key_id, key_version, wrapped_dek, nonce, ct}``; ``is_sealed_secret``), sealed with
  the connection's own context (tenant + connection + purpose: the apply copies the blob into the
  connection row without opening it).
- **An install's completion is journaled with its tokens** (``install``: the install id, the
  connection's identity and grant, the audit payload; no secret), so a crash after the journal and
  before the apply is repaired completely, exactly once: the reconciler applies the tokens AND
  completes the install (connection active, outcome, audit) in one transaction.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0041"
down_revision: str | None = "0040"
branch_labels = None
depends_on = None


def _upgrade(app: str) -> str:
    return f"""
CREATE FUNCTION is_sealed_secret(b bytea) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    doc jsonb;
BEGIN
    doc := convert_from(b, 'UTF8')::jsonb;
    RETURN jsonb_typeof(doc) = 'object' AND doc->>'v' = '1'
       AND doc ?& ARRAY['alg', 'key_id', 'key_version', 'wrapped_dek', 'nonce', 'ct']
       AND (SELECT count(*) FROM jsonb_object_keys(doc)) = 7;
EXCEPTION WHEN others THEN
    RETURN false;
END
$$;

ALTER TABLE token_refresh_journal ADD COLUMN install jsonb;
ALTER TABLE token_refresh_journal ADD CONSTRAINT ck_token_refresh_journal_sealed CHECK (
    is_sealed_secret(encrypted_access_token)
    AND (encrypted_refresh_token IS NULL OR is_sealed_secret(encrypted_refresh_token)));
ALTER TABLE token_refresh_journal ADD CONSTRAINT ck_token_refresh_journal_install CHECK (
    install IS NULL OR jsonb_typeof(install) = 'object');

CREATE FUNCTION guard_token_refresh_journal_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'received' THEN
        RAISE EXCEPTION 'token_refresh_journal: a received entry is never deleted (apply it first)'
            USING ERRCODE = 'EA001';
    END IF;
    RETURN OLD;
END
$$;
DROP TRIGGER trg_token_refresh_journal_no_delete ON token_refresh_journal;
CREATE TRIGGER trg_token_refresh_journal_delete_resolved BEFORE DELETE ON token_refresh_journal
    FOR EACH ROW EXECUTE FUNCTION guard_token_refresh_journal_delete();
GRANT DELETE ON token_refresh_journal TO "{app}";

-- resolved entries of earlier releases: across tenants, inside this transaction only
ALTER TABLE token_refresh_journal NO FORCE ROW LEVEL SECURITY;
DELETE FROM token_refresh_journal WHERE state <> 'received';
ALTER TABLE token_refresh_journal FORCE ROW LEVEL SECURITY;
"""


def _downgrade(app: str) -> str:
    return f"""
REVOKE DELETE ON token_refresh_journal FROM "{app}";
DROP TRIGGER IF EXISTS trg_token_refresh_journal_delete_resolved ON token_refresh_journal;
DROP FUNCTION IF EXISTS guard_token_refresh_journal_delete();
CREATE TRIGGER trg_token_refresh_journal_no_delete BEFORE DELETE ON token_refresh_journal
    FOR EACH ROW EXECUTE FUNCTION reject_mutation();
ALTER TABLE token_refresh_journal DROP CONSTRAINT IF EXISTS ck_token_refresh_journal_install;
ALTER TABLE token_refresh_journal DROP CONSTRAINT IF EXISTS ck_token_refresh_journal_sealed;
ALTER TABLE token_refresh_journal DROP COLUMN IF EXISTS install;
DROP FUNCTION IF EXISTS is_sealed_secret(bytea);
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def _app() -> str:
    return str(op.get_context().config.attributes.get("app_role", "edisc_app"))  # type: ignore[union-attr]


def upgrade() -> None:
    _run(_upgrade(_app()))


def downgrade() -> None:
    _run(_downgrade(_app()))
