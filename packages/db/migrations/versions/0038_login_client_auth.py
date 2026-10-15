"""How our API authenticates itself at a tenant's sign-in IdP (M17 step 3b, plan §1.6).

``tenant_idps.login_client_auth``: ``private_key_jwt`` (the default: a client assertion signed by
the platform key, ``edisc_api.client_auth``) or ``none`` (a public client, PKCE only: refused at
sign-in outside local/test/ci). Rows existing before this revision get the default: production
sign-in is a confidential client from now on.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

from alembic import op

revision = "0038"
down_revision: str | None = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_idps ADD COLUMN login_client_auth text NOT NULL"
        " DEFAULT 'private_key_jwt'"
        " CONSTRAINT ck_tenant_idps_login_client_auth"
        " CHECK (login_client_auth IN ('private_key_jwt', 'none'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_idps DROP COLUMN login_client_auth")
