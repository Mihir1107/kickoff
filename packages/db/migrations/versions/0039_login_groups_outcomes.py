"""Two sign-in outcomes for the IdP's groups claim (before M17 step 4; `edisc_api.idp_groups`).

``groups_overage``: the ID token carried Entra ID's overage marker instead of the groups list.
``groups_missing``: no (usable) groups claim while the tenant grants roles through IdP groups.
Both refuse the sign-in; neither is ever read as "no groups".

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

from alembic import op

revision = "0039"
down_revision: str | None = "0038"
branch_labels = None
depends_on = None

BEFORE = (
    "('completed', 'denied', 'expired', 'invalid', 'unknown_user', 'inactive', 'reauth_mismatch')"
)
AFTER = (
    "('completed', 'denied', 'expired', 'invalid', 'unknown_user', 'inactive', 'reauth_mismatch',"
    " 'groups_overage', 'groups_missing')"
)


def _check(outcomes: str) -> None:
    op.execute("ALTER TABLE login_attempts DROP CONSTRAINT ck_login_attempts_outcome")
    op.execute(
        "ALTER TABLE login_attempts ADD CONSTRAINT ck_login_attempts_outcome CHECK (outcome IS NULL"
        f" OR (consumed_at IS NOT NULL AND outcome IN {outcomes}))"
    )


def upgrade() -> None:
    _check(AFTER)


def downgrade() -> None:
    _check(BEFORE)
