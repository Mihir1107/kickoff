"""Browser sessions (ADR 0016 §1-§4, Amendments 1 and 2; M17 step 2).

- ``tenant_idps.login_client_id``: the OIDC client id our API signs browsers in with at that IdP
  (NULL: bearer tokens only). At most one sign-in IdP per tenant (the login route needs no
  parameter naming one, so nothing about the IdP comes from the request).
- ``login_attempts``: one row per started sign-in. Holds SHA-256 hashes of the state and of the
  login cookie's handle, the nonce, the validated ``return_to``, the re-authentication flag and the
  session the browser held when it started (revoked ``rotated`` at success). The PKCE verifier is
  NOT stored: it is derived from the handle under a server key (``key_id`` names it), so the row
  holds no secret. Insert-only, plus one consume and one outcome (guard trigger).
- ``sessions``: the server side of ``__Host-edisc_session``. Only the SHA-256 of the 256-bit id is
  stored (unique). Identity columns are immutable; ``last_seen_at`` / ``idle_expires_at`` move only
  forward and only while the session is live; revocation happens once and freezes the row; the idle
  expiry never passes the absolute one.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

from alembic import op

from edisc_db.sqlsplit import split_sql

revision = "0036"
down_revision: str | None = "0035"
branch_labels = None
depends_on = None

OUTCOMES = (
    "('completed', 'denied', 'expired', 'invalid', 'unknown_user', 'inactive', 'reauth_mismatch')"
)
REVOKE_REASONS = (
    "('logout', 'self', 'admin', 'principal_deactivated', 'rotated', 'idle', 'absolute')"
)


def _upgrade(app: str) -> str:
    return f"""
ALTER TABLE tenant_idps ADD COLUMN login_client_id text;
ALTER TABLE tenant_idps ADD CONSTRAINT uq_tenant_idps_tenant_id_id UNIQUE (tenant_id, id);
CREATE UNIQUE INDEX uq_tenant_idps_one_login ON tenant_idps (tenant_id)
    WHERE login_client_id IS NOT NULL;

CREATE TABLE sessions (
    id                   uuid NOT NULL,
    tenant_id            uuid NOT NULL,
    principal_id         uuid NOT NULL,
    idp_id               uuid NOT NULL,
    id_hash              bytea NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    authenticated_at     timestamptz NOT NULL,
    last_seen_at         timestamptz NOT NULL DEFAULT now(),
    idle_expires_at      timestamptz NOT NULL,
    absolute_expires_at  timestamptz NOT NULL,
    idp_groups           text[] NOT NULL DEFAULT '{{}}',
    user_agent           text,
    ip_first             text,
    request_id           text,
    revoked_at           timestamptz,
    revoked_by           text,
    revoke_reason        text,
    CONSTRAINT pk_sessions PRIMARY KEY (id),
    CONSTRAINT uq_sessions_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_sessions_id_hash UNIQUE (id_hash),
    CONSTRAINT fk_sessions_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_sessions_tenant_id_principal_id_principals
        FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, id),
    CONSTRAINT fk_sessions_tenant_id_idp_id_tenant_idps
        FOREIGN KEY (tenant_id, idp_id) REFERENCES tenant_idps (tenant_id, id),
    CONSTRAINT ck_sessions_id_hash CHECK (octet_length(id_hash) = 32),
    CONSTRAINT ck_sessions_expiry CHECK (idle_expires_at <= absolute_expires_at
        AND absolute_expires_at > created_at),
    CONSTRAINT ck_sessions_revoked CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL)
        AND (revoked_at IS NULL) = (revoked_by IS NULL)),
    CONSTRAINT ck_sessions_revoke_reason CHECK (revoke_reason IS NULL OR revoke_reason IN {REVOKE_REASONS}),
    CONSTRAINT ck_sessions_user_agent CHECK (user_agent IS NULL OR length(user_agent) <= 256)
);
CREATE INDEX ix_sessions_live_principal ON sessions (tenant_id, principal_id)
    WHERE revoked_at IS NULL;

CREATE OR REPLACE FUNCTION guard_sessions() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.principal_id, NEW.idp_id, NEW.id_hash, NEW.created_at,
        NEW.authenticated_at, NEW.absolute_expires_at, NEW.idp_groups, NEW.user_agent,
        NEW.ip_first, NEW.request_id)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.principal_id, OLD.idp_id, OLD.id_hash, OLD.created_at,
        OLD.authenticated_at, OLD.absolute_expires_at, OLD.idp_groups, OLD.user_agent,
        OLD.ip_first, OLD.request_id) THEN
        RAISE EXCEPTION 'sessions: identity columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'sessions: a revoked session is final' USING ERRCODE = 'EA002';
    END IF;
    IF NEW.last_seen_at < OLD.last_seen_at OR NEW.idle_expires_at < OLD.idle_expires_at THEN
        RAISE EXCEPTION 'sessions: activity never goes back' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sessions USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_sessions_guard BEFORE UPDATE ON sessions FOR EACH ROW EXECUTE FUNCTION guard_sessions();
CREATE TRIGGER trg_sessions_no_delete BEFORE DELETE ON sessions FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON sessions TO "{app}";

CREATE TABLE login_attempts (
    id                uuid NOT NULL,
    tenant_id         uuid NOT NULL,
    idp_id            uuid NOT NULL,
    state_hash        bytea NOT NULL,
    handle_hash       bytea NOT NULL,
    key_id            text NOT NULL,
    nonce             text NOT NULL,
    return_to         text NOT NULL,
    reauth            boolean NOT NULL,
    prior_session_id  uuid,
    started_at        timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz NOT NULL,
    consumed_at       timestamptz,
    outcome           text,
    session_id        uuid,
    CONSTRAINT pk_login_attempts PRIMARY KEY (id),
    CONSTRAINT uq_login_attempts_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_login_attempts_state_hash UNIQUE (state_hash),
    CONSTRAINT fk_login_attempts_tenant_id_tenants FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_login_attempts_tenant_id_idp_id_tenant_idps
        FOREIGN KEY (tenant_id, idp_id) REFERENCES tenant_idps (tenant_id, id),
    CONSTRAINT fk_login_attempts_tenant_id_prior_session_id_sessions
        FOREIGN KEY (tenant_id, prior_session_id) REFERENCES sessions (tenant_id, id),
    CONSTRAINT fk_login_attempts_tenant_id_session_id_sessions
        FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id),
    CONSTRAINT ck_login_attempts_hashes CHECK (octet_length(state_hash) = 32
        AND octet_length(handle_hash) = 32),
    CONSTRAINT ck_login_attempts_return_to CHECK (left(return_to, 1) = '/'
        AND octet_length(return_to) <= 2048),
    CONSTRAINT ck_login_attempts_reauth CHECK (NOT reauth OR prior_session_id IS NOT NULL),
    CONSTRAINT ck_login_attempts_outcome CHECK (outcome IS NULL OR (consumed_at IS NOT NULL
        AND outcome IN {OUTCOMES})),
    CONSTRAINT ck_login_attempts_session CHECK
        ((session_id IS NOT NULL) = (outcome IS NOT DISTINCT FROM 'completed'))
);

CREATE OR REPLACE FUNCTION guard_login_attempts() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.idp_id, NEW.state_hash, NEW.handle_hash, NEW.key_id, NEW.nonce,
        NEW.return_to, NEW.reauth, NEW.prior_session_id, NEW.started_at, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.idp_id, OLD.state_hash, OLD.handle_hash, OLD.key_id, OLD.nonce,
        OLD.return_to, OLD.reauth, OLD.prior_session_id, OLD.started_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'login_attempts: identity columns are immutable' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
        RAISE EXCEPTION 'login_attempts: consumed once' USING ERRCODE = 'EA002';
    END IF;
    IF OLD.outcome IS NOT NULL
       AND (NEW.outcome, NEW.session_id) IS DISTINCT FROM (OLD.outcome, OLD.session_id) THEN
        RAISE EXCEPTION 'login_attempts: the outcome is final' USING ERRCODE = 'EA002';
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON login_attempts USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE TRIGGER trg_login_attempts_guard BEFORE UPDATE ON login_attempts FOR EACH ROW EXECUTE FUNCTION guard_login_attempts();
CREATE TRIGGER trg_login_attempts_no_delete BEFORE DELETE ON login_attempts FOR EACH ROW EXECUTE FUNCTION reject_mutation();
GRANT SELECT, INSERT, UPDATE ON login_attempts TO "{app}";
"""


DOWNGRADE = """
DROP TABLE IF EXISTS login_attempts;
DROP FUNCTION IF EXISTS guard_login_attempts();
DROP TABLE IF EXISTS sessions;
DROP FUNCTION IF EXISTS guard_sessions();
DROP INDEX IF EXISTS uq_tenant_idps_one_login;
ALTER TABLE tenant_idps DROP CONSTRAINT IF EXISTS uq_tenant_idps_tenant_id_id;
ALTER TABLE tenant_idps DROP COLUMN IF EXISTS login_client_id;
"""


def _run(script: str) -> None:
    op.execute("SET LOCAL search_path = edisc, pg_temp")
    for statement in split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _run(_upgrade(str(op.get_context().config.attributes.get("app_role", "edisc_app"))))  # type: ignore[union-attr]


def downgrade() -> None:
    _run(DOWNGRADE)
